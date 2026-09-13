from tests.factories import auth, later


def test_successful_logins_are_ignored(make_pipeline, redis_client):
    p, out, _ = make_pipeline(auth_fail_user_threshold=0)
    assert p.handle_auth(auth(success=True)) == []
    assert redis_client.keys("auth:fail:*") == []


def test_user_scope_fires_above_threshold(make_pipeline):
    p, out, _ = make_pipeline(auth_fail_user_threshold=2, auth_fail_ip_threshold=100)
    ips = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    outcomes = [p.handle_auth(auth(ip=ip, at=later(i))) for i, ip in enumerate(ips)]
    assert outcomes[0] == [] and outcomes[1] == []
    (det,) = outcomes[2]
    assert det.details["scope"] == "user" and det.ip_address == "10.0.0.3"


def test_ip_scope_fires_across_many_users(make_pipeline):
    p, out, _ = make_pipeline(auth_fail_user_threshold=100, auth_fail_ip_threshold=2)
    outcomes = [p.handle_auth(auth(user_id=f"u{i}", ip="203.0.113.9", at=later(i))) for i in range(3)]
    (det,) = outcomes[2]
    assert det.details["scope"] == "ip"


def test_both_scopes_can_fire_on_one_event(make_pipeline):
    p, out, _ = make_pipeline(auth_fail_user_threshold=1, auth_fail_ip_threshold=1)
    p.handle_auth(auth())
    det_user, det_ip = p.handle_auth(auth(at=later(1)))
    assert {det_user.details["scope"], det_ip.details["scope"]} == {"user", "ip"}
    assert det_user.detection_id != det_ip.detection_id


def test_ipv6_keys_are_sanitised(make_pipeline, redis_client):
    p, out, _ = make_pipeline()
    p.handle_auth(auth(ip="2001:db8::1"))
    assert redis_client.exists("auth:fail:ip:2001-db8--1:1m")


def test_window_keys_expire(make_pipeline, redis_client):
    p, out, _ = make_pipeline(auth_fail_key_ttl_seconds=50)
    p.handle_auth(auth())
    assert 0 < redis_client.ttl("auth:fail:user:user_1:1m") <= 50
