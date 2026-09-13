from common.metrics import transactions_processed_total
from tests.factories import LONDON, SF, auth, later, tx


def _counter(outcome):
    return transactions_processed_total.labels(outcome=outcome)._value.get()


def test_clean_transaction_emits_nothing(make_pipeline, kafka_config):
    p, out, _ = make_pipeline()
    p.handle_record(kafka_config.tx_events_topic, 0, 1, tx().model_dump(mode="json"))
    assert out == []


def test_replayed_transaction_is_detected_once_then_cooled_down(make_pipeline, kafka_config):
    p, out, _ = make_pipeline()
    payload = tx().model_dump(mode="json")
    for off in (1, 2, 3):
        p.handle_record(kafka_config.tx_events_topic, 0, off, payload)
    assert [d.detection_type for d in out] == ["replay_attack"]


def test_redelivered_record_produces_no_detection(make_pipeline, kafka_config):
    p, out, _ = make_pipeline()
    payload = tx().model_dump(mode="json")
    p.handle_record(kafka_config.tx_events_topic, 3, 42, payload)
    p.handle_record(kafka_config.tx_events_topic, 3, 42, payload)
    assert out == []


def test_geo_and_burst_can_fire_on_the_same_transaction(make_pipeline):
    # threshold 1 so the SECOND transaction (count 2) is the one that both bursts
    # and travels impossibly — the point is two detectors firing on one record.
    p, out, _ = make_pipeline(burst_threshold=1)
    p.handle_transaction(tx(coords=SF), "r1")
    p.handle_transaction(tx(coords=LONDON, at=later(30)), "r2")
    assert sorted(d.detection_type for d in out) == ["fraud_burst", "geo_velocity_anomaly"]


def test_auth_events_route_to_credential_stuffing(make_pipeline, kafka_config):
    p, out, _ = make_pipeline(auth_fail_user_threshold=1)
    for i in range(3):
        p.handle_record(kafka_config.auth_events_topic, 0, i, auth(at=later(i)).model_dump(mode="json"))
    assert [d.detection_type for d in out] == ["credential_stuffing"]
    assert out[0].details["scope"] == "user"


def test_malformed_payload_is_reported_per_record_not_raised_for_the_batch(make_pipeline, kafka_config):
    p, out, _ = make_pipeline()
    good = tx().model_dump(mode="json")
    result = p.handle_batch([
        (kafka_config.tx_events_topic, 0, 1, {"nope": 1}),
        (kafka_config.tx_events_topic, 0, 2, good),
        (kafka_config.auth_events_topic, 0, 3, {"also": "bad"}),
    ])
    assert set(result.failures) == {0, 2}
    assert result.emitted == []


def test_handle_record_raises_the_permanent_error(make_pipeline, kafka_config):
    import pytest
    p, out, _ = make_pipeline()
    with pytest.raises(Exception):
        p.handle_record(kafka_config.tx_events_topic, 0, 1, {"nope": 1})


def test_batch_mixes_topics_and_keeps_per_user_order(make_pipeline, kafka_config):
    p, out, _ = make_pipeline(burst_threshold=2, auth_fail_user_threshold=1)
    t = kafka_config.tx_events_topic; a = kafka_config.auth_events_topic
    records = [
        (t, 0, 1, tx(at=later(0)).model_dump(mode="json")),
        (a, 1, 1, auth(at=later(0)).model_dump(mode="json")),
        (t, 0, 2, tx(at=later(1)).model_dump(mode="json")),
        (a, 1, 2, auth(at=later(1)).model_dump(mode="json")),
        (t, 0, 3, tx(at=later(2)).model_dump(mode="json")),  # 3rd tx in window -> burst
    ]
    result = p.handle_batch(records)
    assert result.failures == {}
    assert sorted(d.detection_type for d in result.emitted) == ["credential_stuffing", "fraud_burst"]


def test_batch_costs_a_fixed_number_of_round_trips(make_pipeline, kafka_config, redis_client):
    """500 clean transactions: one enforcement pipeline + one check pipeline."""
    calls = []
    original = redis_client.pipeline
    redis_client.pipeline = lambda *a, **kw: (calls.append(1), original(*a, **kw))[1]
    p, out, _ = make_pipeline()
    records = [(kafka_config.tx_events_topic, 0, i, tx(user_id=f"u{i}").model_dump(mode="json")) for i in range(500)]
    p.handle_batch(records)
    assert len(calls) == 2
    assert out == []


def test_outcome_counter_counts_suppressed_detections_as_detected(make_pipeline, kafka_config):
    p, out, _ = make_pipeline()
    before = _counter("detected")
    payload = tx().model_dump(mode="json")
    for off in (1, 2, 3):
        p.handle_record(kafka_config.tx_events_topic, 0, off, payload)
    assert _counter("detected") - before == 2
    assert len(out) == 1


def test_detection_ids_are_stable_across_redelivery(make_pipeline, kafka_config):
    """The same Kafka delivery reprocessed yields the same detection id, so
    containment (keyed by id) applies it exactly once."""
    p, out, _ = make_pipeline(burst_threshold=0, detection_cooldown_seconds=0)
    rec = (kafka_config.tx_events_topic, 5, 42, tx(user_id="u1").model_dump(mode="json"))
    first = p.handle_batch([rec]).emitted
    second = p.handle_batch([rec]).emitted  # redelivery: same topic/partition/offset
    assert first and second
    assert {d.detection_id for d in first} == {d.detection_id for d in second}


def test_different_deliveries_get_different_ids(make_pipeline, kafka_config):
    p, out, _ = make_pipeline(burst_threshold=0, detection_cooldown_seconds=0)
    a = p.handle_batch([(kafka_config.tx_events_topic, 0, 1, tx(user_id="u1").model_dump(mode="json"))]).emitted
    b = p.handle_batch([(kafka_config.tx_events_topic, 0, 2, tx(user_id="u1").model_dump(mode="json"))]).emitted
    assert {d.detection_id for d in a}.isdisjoint({d.detection_id for d in b})


def test_batch_result_reports_claimed_cooldown_keys(make_pipeline, kafka_config):
    p, out, _ = make_pipeline(burst_threshold=0)
    result = p.handle_batch([(kafka_config.tx_events_topic, 0, 1, tx(user_id="u1").model_dump(mode="json"))])
    assert result.claimed_cooldown_keys  # burst detection claimed a cooldown
    # Releasing them lets the same detection be re-emitted on a retry.
    p.release_cooldown(result.claimed_cooldown_keys)
    again = p.handle_batch([(kafka_config.tx_events_topic, 0, 1, tx(user_id="u1").model_dump(mode="json"))]).emitted
    assert [d.detection_type for d in again] == ["fraud_burst"]


def test_cooldown_suppresses_retry_without_release(make_pipeline, kafka_config):
    """Guards the bug this fix targets: without releasing the claim, a reprocess
    is suppressed (which is why the consumer releases on delivery failure)."""
    p, out, _ = make_pipeline(burst_threshold=0)
    rec = (kafka_config.tx_events_topic, 0, 1, tx(user_id="u1").model_dump(mode="json"))
    assert p.handle_batch([rec]).emitted  # first emits + claims cooldown
    assert p.handle_batch([rec]).emitted == []  # reprocess suppressed by the claim
