from dataclasses import replace
from datetime import datetime, timezone

from common.models import DetectionEvent
from containment_engine.containment_actions import apply_containment
from containment_engine.pipeline import ContainmentPipeline


def _det(dtype, user="user_1", ip=None, scope=None, det_id=None):
    return DetectionEvent(
        detection_id=det_id or f"det-{dtype}",
        detection_type=dtype,
        severity="high",
        user_id=user,
        transaction_id="t",
        timestamp=datetime(2025, 3, 8, tzinfo=timezone.utc),
        ip_address=ip,
        details={"scope": scope} if scope else {},
    )


def test_replay_quarantines_user_with_ttl(redis_client, redis_config):
    cfg = replace(redis_config, quarantine_ttl_seconds=100)
    assert apply_containment(_det("replay_attack"), redis_client, cfg) == ["quarantine"]
    assert redis_client.get("enforce:quarantine:user:user_1") == "det-replay_attack"
    assert 0 < redis_client.ttl("enforce:quarantine:user:user_1") <= 100


def test_ip_scope_credential_stuffing_quarantines_and_blocks_ip(redis_client, redis_config):
    det = _det("credential_stuffing", ip="2001:db8::7", scope="ip")
    assert apply_containment(det, redis_client, redis_config) == ["quarantine", "ip_block"]
    assert redis_client.exists("block:ip:2001-db8--7")


def test_user_scope_credential_stuffing_never_blocks_the_triggering_ip(redis_client, redis_config):
    """The IP on a user-scope detection is whoever tipped the count, possibly the real user."""
    det = _det("credential_stuffing", ip="198.51.100.10", scope="user")
    assert apply_containment(det, redis_client, redis_config) == ["quarantine"]
    assert redis_client.keys("block:ip:*") == []


def test_ip_scope_without_ip_only_quarantines(redis_client, redis_config):
    det = _det("credential_stuffing", scope="ip")
    assert apply_containment(det, redis_client, redis_config) == ["quarantine"]
    assert redis_client.keys("block:ip:*") == []


def test_unknown_type_does_nothing(redis_client, redis_config):
    assert apply_containment(_det("mystery"), redis_client, redis_config) == []
    assert redis_client.keys("*") == []


def test_reapplying_is_idempotent(redis_client, redis_config):
    for _ in range(3):
        apply_containment(_det("fraud_burst"), redis_client, redis_config)
    assert redis_client.keys("enforce:quarantine:user:*") == ["enforce:quarantine:user:user_1"]


def test_redelivered_detection_does_not_extend_ttl(redis_client, redis_config):
    """A redelivered detection (same id) returns the original actions and must
    NOT refresh the rule's TTL — otherwise Kafka redelivery would keep a
    quarantine alive indefinitely."""
    cfg = replace(redis_config, quarantine_ttl_seconds=100)
    det = _det("fraud_burst", det_id="fixed-1")
    assert apply_containment(det, redis_client, cfg) == ["quarantine"]
    redis_client.expire("enforce:quarantine:user:user_1", 10)  # simulate time passing
    assert apply_containment(det, redis_client, cfg) == ["quarantine"]  # same actions
    assert redis_client.ttl("enforce:quarantine:user:user_1") <= 10  # not refreshed to 100


def test_redelivered_geo_anomaly_does_not_escalate(redis_client, redis_config):
    """The SAME geo detection redelivered must not turn its own step-up into a
    quarantine; only a genuinely different anomaly escalates."""
    det = _det("geo_velocity_anomaly", det_id="geo-1")
    assert apply_containment(det, redis_client, redis_config) == ["step_up_auth"]
    assert apply_containment(det, redis_client, redis_config) == ["step_up_auth"]
    assert redis_client.exists("enforce:quarantine:user:user_1") == 0


def test_two_distinct_geo_anomalies_escalate(redis_client, redis_config):
    assert apply_containment(_det("geo_velocity_anomaly", det_id="geo-1"), redis_client, redis_config) == ["step_up_auth"]
    assert apply_containment(_det("geo_velocity_anomaly", det_id="geo-2"), redis_client, redis_config) == ["step_up_auth", "quarantine"]
    assert redis_client.exists("enforce:quarantine:user:user_1")


def test_receipt_records_the_actions(redis_client, redis_config):
    import json
    apply_containment(_det("credential_stuffing", ip="203.0.113.5", scope="ip", det_id="cs-1"), redis_client, redis_config)
    assert json.loads(redis_client.get("containment:applied:cs-1")) == ["quarantine", "ip_block"]


def test_pipeline_emits_audit_record(redis_client, redis_config):
    audits = []
    p = ContainmentPipeline(redis_client, audits.append, redis_config)
    det = _det("credential_stuffing", ip="203.0.113.5", scope="ip")
    p.handle_record("detections", 0, 0, det.model_dump(mode="json"))
    (audit,) = audits
    assert audit["action"] == "quarantine+ip_block"
    assert audit["actions"] == ["quarantine", "ip_block"]
    assert audit["detection_id"] == det.detection_id
    assert audit["details"]["scope"] == "ip"
