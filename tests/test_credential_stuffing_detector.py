from dataclasses import replace

from detection_engine.credential_stuffing_detector import check_credential_stuffing
from tests.factories import auth, later


def test_successful_logins_are_ignored(redis_client, redis_config):
    cfg = replace(redis_config, auth_fail_user_threshold=0)
    assert check_credential_stuffing(auth(success=True), redis_client, cfg) == (None, None)
    assert redis_client.keys("auth:fail:*") == []


def test_user_scope_fires_above_threshold(redis_client, redis_config):
    cfg = replace(redis_config, auth_fail_user_threshold=2, auth_fail_ip_threshold=100)
    ips = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    outcomes = [check_credential_stuffing(auth(ip=ip, at=later(i)), redis_client, cfg) for i, ip in enumerate(ips)]
    assert outcomes[0] == (None, None) and outcomes[1] == (None, None)
    det_user, det_ip = outcomes[2]
    assert det_user is not None and det_ip is None
    assert det_user.details["scope"] == "user"
    assert det_user.ip_address == "10.0.0.3"


def test_ip_scope_fires_across_many_users(redis_client, redis_config):
    cfg = replace(redis_config, auth_fail_user_threshold=100, auth_fail_ip_threshold=2)
    outcomes = [
        check_credential_stuffing(auth(user_id=f"u{i}", ip="203.0.113.9", at=later(i)), redis_client, cfg)
        for i in range(3)
    ]
    det_user, det_ip = outcomes[2]
    assert det_user is None and det_ip is not None
    assert det_ip.details["scope"] == "ip"


def test_both_scopes_can_fire_on_one_event(redis_client, redis_config):
    cfg = replace(redis_config, auth_fail_user_threshold=1, auth_fail_ip_threshold=1)
    check_credential_stuffing(auth(), redis_client, cfg)
    det_user, det_ip = check_credential_stuffing(auth(at=later(1)), redis_client, cfg)
    assert det_user and det_ip
    assert det_user.detection_id != det_ip.detection_id


def test_ipv6_keys_are_sanitised(redis_client, redis_config):
    check_credential_stuffing(auth(ip="2001:db8::1"), redis_client, redis_config)
    assert redis_client.exists("auth:fail:ip:2001-db8--1:1m")


def test_whole_check_is_one_round_trip(redis_client, redis_config):
    calls = []
    original = redis_client.pipeline

    def counting_pipeline(*a, **kw):
        calls.append(1)
        return original(*a, **kw)

    redis_client.pipeline = counting_pipeline
    check_credential_stuffing(auth(), redis_client, redis_config)
    assert len(calls) == 1
