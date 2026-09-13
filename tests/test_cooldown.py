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


def test_filter_returns_claimed_keys_and_release_reenables(redis_client, redis_config):
    from detection_engine.cooldown import cooldown_key, filter_cooled_down, release_cooldown

    d = _det("fraud_burst", "u1")
    allowed, claimed = filter_cooled_down([d], redis_client, redis_config)
    assert allowed == [d]
    assert claimed == [cooldown_key(d)]
    assert redis_client.exists(cooldown_key(d))

    # A repeat is suppressed and claims nothing.
    allowed2, claimed2 = filter_cooled_down([_det("fraud_burst", "u1")], redis_client, redis_config)
    assert allowed2 == [] and claimed2 == []

    # Releasing the claim lets the next call win again (the delivery-failed path).
    release_cooldown(claimed, redis_client)
    assert not redis_client.exists(cooldown_key(d))
    allowed3, _ = filter_cooled_down([_det("fraud_burst", "u1")], redis_client, redis_config)
    assert len(allowed3) == 1


def test_zero_cooldown_claims_nothing(redis_client, redis_config):
    from detection_engine.cooldown import filter_cooled_down

    cfg = replace(redis_config, detection_cooldown_seconds=0)
    allowed, claimed = filter_cooled_down([_det(), _det()], redis_client, cfg)
    assert len(allowed) == 2 and claimed == []
