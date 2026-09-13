from dataclasses import replace

import pytest

from common import session_cache
from common.metrics import transactions_processed_total
from configs.kafka_config import KafkaConfig
from detection_engine.pipeline import DetectionPipeline
from tests.factories import LONDON, SF, auth, later, tx


@pytest.fixture(autouse=True)
def _fresh_cache():
    session_cache.clear()
    yield
    session_cache.clear()


@pytest.fixture
def kafka_config():
    return KafkaConfig.from_env()


def _pipeline(redis_client, redis_config, kafka_config, **overrides):
    out = []
    p = DetectionPipeline(redis_client, out.append, kafka_config, replace(redis_config, **overrides))
    return p, out


def test_clean_transaction_emits_nothing(redis_client, redis_config, kafka_config):
    p, out = _pipeline(redis_client, redis_config, kafka_config)
    p.handle_record(kafka_config.tx_events_topic, 0, 1, tx().model_dump(mode="json"))
    assert out == []


def test_replayed_transaction_is_detected_once_then_cooled_down(redis_client, redis_config, kafka_config):
    p, out = _pipeline(redis_client, redis_config, kafka_config)
    payload = tx().model_dump(mode="json")
    p.handle_record(kafka_config.tx_events_topic, 0, 1, payload)
    p.handle_record(kafka_config.tx_events_topic, 0, 2, payload)
    p.handle_record(kafka_config.tx_events_topic, 0, 3, payload)
    assert [d.detection_type for d in out] == ["replay_attack"]


def test_redelivered_record_produces_no_detection(redis_client, redis_config, kafka_config):
    p, out = _pipeline(redis_client, redis_config, kafka_config)
    payload = tx().model_dump(mode="json")
    p.handle_record(kafka_config.tx_events_topic, 3, 42, payload)
    p.handle_record(kafka_config.tx_events_topic, 3, 42, payload)
    assert out == []


def test_geo_and_burst_can_fire_on_the_same_transaction(redis_client, redis_config, kafka_config):
    p, out = _pipeline(redis_client, redis_config, kafka_config, burst_threshold=1)
    p.handle_transaction(tx(coords=SF), "r1")
    p.handle_transaction(tx(coords=LONDON, at=later(30)), "r2")
    assert sorted(d.detection_type for d in out) == ["fraud_burst", "geo_velocity_anomaly"]


def test_auth_events_route_to_credential_stuffing(redis_client, redis_config, kafka_config):
    p, out = _pipeline(redis_client, redis_config, kafka_config, auth_fail_user_threshold=1)
    for i in range(3):
        p.handle_record(kafka_config.auth_events_topic, 0, i, auth(at=later(i)).model_dump(mode="json"))
    assert [d.detection_type for d in out] == ["credential_stuffing"]
    assert out[0].details["scope"] == "user"


def test_malformed_payload_raises_for_the_caller_to_handle(redis_client, redis_config, kafka_config):
    p, _ = _pipeline(redis_client, redis_config, kafka_config)
    with pytest.raises(Exception):
        p.handle_record(kafka_config.tx_events_topic, 0, 1, {"nope": 1})


def test_outcome_counter_counts_suppressed_detections_as_detected(redis_client, redis_config, kafka_config):
    p, out = _pipeline(redis_client, redis_config, kafka_config)
    before = transactions_processed_total.labels(outcome="detected")._value.get()
    payload = tx().model_dump(mode="json")
    p.handle_record(kafka_config.tx_events_topic, 0, 1, payload)
    p.handle_record(kafka_config.tx_events_topic, 0, 2, payload)
    p.handle_record(kafka_config.tx_events_topic, 0, 3, payload)  # suppressed by cooldown
    after = transactions_processed_total.labels(outcome="detected")._value.get()
    assert after - before == 2
    assert len(out) == 1
