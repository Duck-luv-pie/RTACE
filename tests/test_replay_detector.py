
from detection_engine.replay_detector import replay_key
from tests.factories import tx


def _types(dets):
    return [d.detection_type for d in dets]


def test_first_sighting_is_clean(make_pipeline):
    p, out, _ = make_pipeline()
    assert p.handle_transaction(tx(), "tx:0:1") == []


def test_same_payload_in_a_different_record_is_a_replay(make_pipeline):
    p, out, _ = make_pipeline()
    original = tx()
    p.handle_transaction(original, "tx:0:1")
    (det,) = p.handle_transaction(original, "tx:0:9")
    assert det.detection_type == "replay_attack"
    assert det.transaction_id == original.event_id
    assert det.details["first_seen_record"] == "tx:0:1"


def test_redelivery_of_the_same_record_is_not_a_replay(make_pipeline):
    p, out, _ = make_pipeline()
    original = tx()
    p.handle_transaction(original, "tx:0:1")
    assert p.handle_transaction(original, "tx:0:1") == []


def test_replay_after_redelivery_is_still_caught(make_pipeline):
    p, out, _ = make_pipeline(detection_cooldown_seconds=0)
    original = tx()
    p.handle_transaction(original, "tx:0:1")
    p.handle_transaction(original, "tx:0:1")
    assert _types(p.handle_transaction(original, "tx:0:2")) == ["replay_attack"]


def test_without_record_ref_every_repeat_is_a_replay(make_pipeline):
    p, out, _ = make_pipeline()
    original = tx()
    assert p.handle_transaction(original) == []
    assert _types(p.handle_transaction(original)) == ["replay_attack"]


def test_detection_ids_are_unique_per_detection(make_pipeline):
    p, out, _ = make_pipeline(detection_cooldown_seconds=0)
    original = tx()
    p.handle_transaction(original, "a")
    (d1,) = p.handle_transaction(original, "b")
    (d2,) = p.handle_transaction(original, "c")
    assert d1.detection_id != d2.detection_id


def test_key_has_sliding_ttl_and_is_not_refreshed_by_repeats(make_pipeline, redis_client):
    p, out, _ = make_pipeline(replay_ttl_hours=2)
    original = tx()
    p.handle_transaction(original, "a")
    ttl_before = redis_client.ttl(replay_key(original))
    assert 0 < ttl_before <= 2 * 3600
    p.handle_transaction(original, "b")
    assert redis_client.ttl(replay_key(original)) <= ttl_before


def test_different_transactions_do_not_collide(make_pipeline):
    p, out, _ = make_pipeline()
    p.handle_transaction(tx(amount=10.0), "a")
    assert p.handle_transaction(tx(amount=10.01), "b") == []


def test_prescored_event_is_not_reprocessed_and_reemits_decision_findings(make_pipeline, redis_client):
    """The decision service scored this event first: its findings are re-emitted,
    and burst/session state is not advanced a second time."""
    import json
    p, out, _ = make_pipeline()
    t = tx()
    redis_client.set(replay_key(t), f"decision:{t.event_id}")
    redis_client.set(f"decision:result:{t.event_id}", json.dumps({
        "verdict": "DENY", "score": 100,
        "reasons": [{"type": "replay_attack", "details": {"first_seen_record": "tx:0:1"}}],
    }))
    (det,) = p.handle_transaction(t, "tx:0:5")
    assert det.detection_type == "replay_attack"
    assert det.details["source"] == "decision-service" and det.details["verdict"] == "DENY"
    assert redis_client.exists("session:user_1") == 0
    assert redis_client.zcard("burst:user_1:1m") == 0
