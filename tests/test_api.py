import fakeredis
import pytest
from fastapi.testclient import TestClient

from api import main as api_main


@pytest.fixture
def client(monkeypatch):
    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(api_main, "create_redis_client", lambda cfg=None: fake)
    monkeypatch.setenv("RTACE_API_TOKEN", "s3cret")
    monkeypatch.setattr(api_main, "_audit", lambda *a, **k: None)
    with TestClient(api_main.app) as c:
        c.redis = fake
        yield c


@pytest.fixture
def open_client(monkeypatch):
    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(api_main, "create_redis_client", lambda cfg=None: fake)
    monkeypatch.delenv("RTACE_API_TOKEN", raising=False)
    with TestClient(api_main.app) as c:
        yield c


AUTH = {"Authorization": "Bearer s3cret"}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "redis": "connected"}


def test_rules_require_token_when_configured(client):
    assert client.get("/enforcement/rules").status_code == 401
    assert client.get("/enforcement/rules", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/enforcement/rules", headers=AUTH).status_code == 200


def test_rules_list_all_three_kinds_with_ttl_and_origin(client):
    client.redis.setex("enforce:quarantine:user:u1", 100, "det-1")
    client.redis.setex("enforce:step_up:user:u2", 50, "det-2")
    client.redis.setex("block:ip:2001-db8--9", 30, "det-3")
    body = client.get("/enforcement/rules", headers=AUTH).json()
    assert body["count"] == 3
    (q,) = body["quarantines"]
    assert q["user_id"] == "u1" and q["set_by"] == "det-1" and 0 < q["ttl_seconds"] <= 100
    assert body["step_ups"][0]["user_id"] == "u2"
    assert body["ip_blocks"][0]["ip"] == "2001:db8::9"


def test_manual_quarantine_and_lift(client):
    r = client.post("/enforcement/quarantine/u9?ttl_seconds=120&reason=fraud-desk", headers=AUTH)
    assert r.status_code == 201 and r.json()["ttl_seconds"] == 120
    assert client.redis.get("enforce:quarantine:user:u9") == "api:fraud-desk"
    assert client.delete("/enforcement/quarantine/u9", headers=AUTH).status_code == 200
    assert client.redis.exists("enforce:quarantine:user:u9") == 0
    assert client.delete("/enforcement/quarantine/u9", headers=AUTH).status_code == 404


def test_ip_block_and_step_up_overrides(client):
    assert client.post("/enforcement/ip-block/203.0.113.7", headers=AUTH).status_code == 201
    assert client.redis.exists("block:ip:203.0.113.7")
    assert client.delete("/enforcement/ip-block/203.0.113.7", headers=AUTH).status_code == 200
    client.redis.set("enforce:step_up:user:u1", "det")
    assert client.delete("/enforcement/step-up/u1", headers=AUTH).status_code == 200
    assert client.redis.exists("enforce:step_up:user:u1") == 0


def test_writes_are_disabled_without_a_token(open_client):
    assert open_client.get("/enforcement/rules").status_code == 200
    r = open_client.post("/enforcement/quarantine/u1")
    assert r.status_code == 503
    assert "RTACE_API_TOKEN" in r.json()["detail"]


def test_invalid_ttl_is_rejected(client):
    assert client.post("/enforcement/quarantine/u1?ttl_seconds=0", headers=AUTH).status_code == 422
