from dataclasses import replace
from datetime import datetime, timezone

from common.models import DetectionEvent
from containment_engine.containment_actions import apply_containment
from containment_engine.pipeline import ContainmentPipeline


def _det(dtype, user="user_1", ip=None, scope=None):
    return DetectionEvent(
        detection_id=f"det-{dtype}",
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


def test_credential_stuffing_quarantines_and_blocks_ip(redis_client, redis_config):
    actions = apply_containment(_det("credential_stuffing", ip="2001:db8::7"), redis_client, redis_config)
    assert actions == ["quarantine", "ip_block"]
    assert redis_client.exists("block:ip:2001-db8--7")


def test_credential_stuffing_without_ip_only_quarantines(redis_client, redis_config):
    assert apply_containment(_det("credential_stuffing"), redis_client, redis_config) == ["quarantine"]
    assert redis_client.keys("block:ip:*") == []


def test_unknown_type_does_nothing(redis_client, redis_config):
    assert apply_containment(_det("mystery"), redis_client, redis_config) == []
    assert redis_client.keys("*") == []


def test_reapplying_is_idempotent(redis_client, redis_config):
    for _ in range(3):
        apply_containment(_det("fraud_burst"), redis_client, redis_config)
    assert redis_client.keys("enforce:quarantine:user:*") == ["enforce:quarantine:user:user_1"]


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
