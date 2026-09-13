from dataclasses import replace
from datetime import datetime, timezone

from common.models import DetectionEvent
from detection_engine.cooldown import cooldown_subject, should_emit


def _det(dtype="fraud_burst", user="user_1", ip=None, scope=None):
    return DetectionEvent(
        detection_id="x",
        detection_type=dtype,
        severity="high",
        user_id=user,
        transaction_id="t",
        timestamp=datetime.now(timezone.utc),
        ip_address=ip,
        details={"scope": scope} if scope else {},
    )


def test_first_detection_passes_then_repeats_are_suppressed(redis_client, redis_config):
    assert should_emit(_det(), redis_client, redis_config)
    assert not should_emit(_det(), redis_client, redis_config)
    assert not should_emit(_det(), redis_client, redis_config)


def test_cooldown_is_per_type_and_subject(redis_client, redis_config):
    assert should_emit(_det("fraud_burst", "a"), redis_client, redis_config)
    assert should_emit(_det("replay_attack", "a"), redis_client, redis_config)
    assert should_emit(_det("fraud_burst", "b"), redis_client, redis_config)


def test_ip_scope_detections_are_keyed_by_ip(redis_client, redis_config):
    d1 = _det("credential_stuffing", "u1", ip="2001:db8::1", scope="ip")
    d2 = _det("credential_stuffing", "u2", ip="2001:db8::1", scope="ip")
    assert cooldown_subject(d1) == "ip:2001-db8--1"
    assert should_emit(d1, redis_client, redis_config)
    assert not should_emit(d2, redis_client, redis_config)
    # user-scope for one of those users is a different subject
    assert should_emit(_det("credential_stuffing", "u1", ip="2001:db8::1", scope="user"), redis_client, redis_config)


def test_zero_cooldown_disables_suppression(redis_client, redis_config):
    cfg = replace(redis_config, detection_cooldown_seconds=0)
    assert should_emit(_det(), redis_client, cfg)
    assert should_emit(_det(), redis_client, cfg)


def test_cooldown_key_expires(redis_client, redis_config):
    cfg = replace(redis_config, detection_cooldown_seconds=30)
    should_emit(_det(), redis_client, cfg)
    assert 0 < redis_client.ttl("cooldown:fraud_burst:user:user_1") <= 30
