"""Enforcement: blocked events in the detection pipeline and the tiered containment policy."""

from dataclasses import replace

import pytest

from common import session_cache
from common.enforcement import ip_block_key, quarantine_key, step_up_key
from configs.kafka_config import KafkaConfig
from containment_engine.containment_actions import apply_containment
from detection_engine.pipeline import DetectionPipeline
from tests.factories import LONDON, SF, auth, later, tx
from tests.test_containment import _det


@pytest.fixture(autouse=True)
def _fresh_cache():
    session_cache.clear()
    yield
    session_cache.clear()


def _pipeline(redis_client, redis_config):
    out, audits = [], []
    p = DetectionPipeline(redis_client, out.append, KafkaConfig.from_env(), redis_config, emit_audit=audits.append)
    return p, out, audits


def test_quarantined_user_transactions_are_refused_and_not_analysed(redis_client, redis_config):
    p, out, audits = _pipeline(redis_client, redis_config)
    redis_client.setex(quarantine_key("user_1"), 60, "det-x")
    payload = tx().model_dump(mode="json")
    p.handle_record("tx-events", 0, 1, payload)
    p.handle_record("tx-events", 0, 2, payload)  # would be a replay if analysed
    assert out == []
    assert redis_client.keys("replay:seen:*") == []
    assert redis_client.exists("session:user_1") == 0
    assert [a["event"] for a in audits] == ["transaction_blocked", "transaction_blocked"]
    assert audits[0]["reason"] == "quarantine"


def test_blocked_ip_login_attempts_do_not_feed_stuffing_counters(redis_client, redis_config):
    cfg = replace(redis_config, auth_fail_user_threshold=1)
    p, out, audits = _pipeline(redis_client, cfg)
    redis_client.setex(ip_block_key("2001:db8::1"), 60, "det-x")
    for i in range(5):
        p.handle_auth(auth(ip="2001:db8::1", at=later(i)))
    assert out == []
    assert redis_client.keys("auth:fail:*") == []
    assert audits[0]["event"] == "auth_blocked" and audits[0]["ip_address"] == "2001:db8::1"


def test_step_up_does_not_block_transactions(redis_client, redis_config):
    p, out, _ = _pipeline(redis_client, redis_config)
    redis_client.setex(step_up_key("user_1"), 60, "det-x")
    p.handle_transaction(tx(coords=SF), "r1")
    assert redis_client.exists("session:user_1") == 1


def test_geo_anomaly_requires_step_up_not_quarantine(redis_client, redis_config):
    actions = apply_containment(_det("geo_velocity_anomaly"), redis_client, redis_config)
    assert actions == ["step_up_auth"]
    assert redis_client.exists(step_up_key("user_1"))
    assert redis_client.exists(quarantine_key("user_1")) == 0
    assert 0 < redis_client.ttl(step_up_key("user_1")) <= redis_config.step_up_ttl_seconds


def test_second_geo_anomaly_while_step_up_pending_escalates(redis_client, redis_config):
    apply_containment(_det("geo_velocity_anomaly"), redis_client, redis_config)
    actions = apply_containment(_det("geo_velocity_anomaly"), redis_client, redis_config)
    assert actions == ["step_up_auth", "quarantine"]
    assert redis_client.exists(quarantine_key("user_1"))


def test_hard_signals_still_quarantine(redis_client, redis_config):
    assert apply_containment(_det("replay_attack"), redis_client, redis_config) == ["quarantine"]
    assert apply_containment(_det("fraud_burst", user="u2"), redis_client, redis_config) == ["quarantine"]


def test_end_to_end_geo_then_blocked_after_escalation(redis_client, redis_config):
    """Detection -> containment -> enforcement, all against one Redis."""
    cfg = replace(redis_config, detection_cooldown_seconds=0)
    p, out, audits = _pipeline(redis_client, cfg)
    p.handle_transaction(tx(coords=SF), "r1")
    p.handle_transaction(tx(coords=LONDON, at=later(600)), "r2")
    assert apply_containment(out[-1], redis_client, cfg) == ["step_up_auth"]
    p.handle_transaction(tx(coords=LONDON, at=later(1200)), "r3")
    assert apply_containment(out[-1], redis_client, cfg) == ["step_up_auth", "quarantine"]
    p.handle_transaction(tx(coords=SF, at=later(1800)), "r4")
    assert audits[-1]["event"] == "transaction_blocked"
