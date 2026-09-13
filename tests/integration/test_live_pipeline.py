"""End-to-end tests against a live Kafka + Redis (and optionally the Go decision service).

Each test creates unique topics and uses an isolated Redis DB so a running stack
is untouched. It exercises the real consumer loop, the real Lua scripts in real
Redis, and real Kafka round trips.
"""

import json
import os
import threading
import time
import uuid
from dataclasses import replace

import pytest
import redis as redis_lib
from kafka import KafkaConsumer
from kafka.admin import KafkaAdminClient, NewTopic
from kafka.errors import TopicAlreadyExistsError

from common.consumer_loop import LoopSettings, run_consumer_loop
from common.enforcement import quarantine_key
from common.kafka_client import create_consumer, create_producer, send_message
from common.models import AuthEvent, DetectionEvent, TransactionEvent
from configs.kafka_config import KafkaConfig
from configs.redis_config import RedisConfig
from detection_engine.pipeline import DetectionPipeline

BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
TEST_DB = int(os.getenv("RTACE_TEST_REDIS_DB", "14"))
T0 = 1_700_000_000.0  # fixed base unix time so windows are deterministic

SF = (37.77, -122.42)
LONDON = (51.51, -0.13)


def _tx(user, coords=SF, at=T0, event_id=None, amount=42.5):
    from datetime import datetime, timezone
    return TransactionEvent(
        event_id=event_id or f"evt-{uuid.uuid4()}", user_id=user, amount=amount, merchant="Amazon",
        timestamp=datetime.fromtimestamp(at, tz=timezone.utc), location="US-CA",
        latitude=coords[0], longitude=coords[1],
    )


def _auth(user, ip, at=T0, event_id=None, success=False):
    from datetime import datetime, timezone
    return AuthEvent(event_id=event_id or f"a-{uuid.uuid4()}", user_id=user, ip_address=ip,
                     success=success, timestamp=datetime.fromtimestamp(at, tz=timezone.utc))


@pytest.fixture
def redis_client():
    r = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, db=TEST_DB, decode_responses=True)
    r.flushdb()
    try:
        yield r
    finally:
        r.flushdb()
        r.close()


@pytest.fixture
def topics():
    """Unique topic set for this run; created and dropped around the test."""
    run = uuid.uuid4().hex[:8]
    names = {k: f"it-{k}-{run}" for k in ("tx", "auth", "det", "dlq", "audit")}
    admin = KafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    new = [NewTopic(name=n, num_partitions=1, replication_factor=1) for n in names.values()]
    try:
        admin.create_topics(new)
    except TopicAlreadyExistsError:
        pass
    # Wait until every topic is visible to the cluster metadata.
    deadline = time.time() + 20
    while time.time() < deadline:
        if set(names.values()).issubset(set(admin.list_topics())):
            break
        time.sleep(0.5)
    try:
        yield names
    finally:
        admin.delete_topics(list(names.values()))
        admin.close()


def _kafka_config(topics) -> KafkaConfig:
    base = KafkaConfig.from_env()
    return replace(base, bootstrap_servers=BOOTSTRAP, tx_events_topic=topics["tx"],
                   auth_events_topic=topics["auth"], detections_topic=topics["det"],
                   audit_log_topic=topics["audit"], dlq_topic=topics["dlq"])


def _run_engine(kcfg, rcfg, redis_client, shutdown):
    """Run the real consumer loop with a DetectionPipeline, emitting to Kafka."""
    consumer = create_consumer([kcfg.tx_events_topic, kcfg.auth_events_topic],
                               group_id=f"it-eng-{uuid.uuid4().hex[:8]}", config=kcfg)
    producer = create_producer(kcfg)

    def emit(d: DetectionEvent):
        send_message(producer, kcfg.detections_topic, d.model_dump(mode="json"), key=d.user_id)

    def emit_audit(a: dict):
        send_message(producer, kcfg.audit_log_topic, a, key=a.get("user_id"))

    pipeline = DetectionPipeline(redis_client, emit, kcfg, rcfg, emit_audit=emit_audit)

    def handle(records):
        return pipeline.handle_batch(records).failures

    run_consumer_loop(consumer, handle, LoopSettings(stage="it-detection", poll_timeout_ms=500),
                      shutdown, producer=producer, dlq_topic=kcfg.dlq_topic)


def _collect_detections(kcfg, expected, timeout=25):
    """Read the detections topic until `expected` count arrives or timeout."""
    c = KafkaConsumer(kcfg.detections_topic, bootstrap_servers=BOOTSTRAP,
                      auto_offset_reset="earliest", enable_auto_commit=False,
                      value_deserializer=lambda m: json.loads(m.decode()),
                      consumer_timeout_ms=1000, group_id=f"it-collect-{uuid.uuid4().hex[:8]}")
    out, deadline = [], time.time() + timeout
    try:
        while time.time() < deadline and len(out) < expected:
            for msg in c:
                out.append(msg.value)
                if len(out) >= expected:
                    break
    finally:
        c.close()
    return out


def test_replay_and_burst_detected_end_to_end(redis_client, topics):
    kcfg = _kafka_config(topics)
    rcfg = replace(RedisConfig.from_env(), burst_threshold=3, detection_cooldown_seconds=0)
    shutdown = threading.Event()
    engine = threading.Thread(target=_run_engine, args=(kcfg, rcfg, redis_client, shutdown), daemon=True)
    engine.start()
    try:
        producer = create_producer(kcfg)
        user = f"user-{uuid.uuid4().hex[:6]}"
        # A replay: the identical transaction produced as two separate records.
        replayed = _tx(user, amount=99.0)
        for _ in range(2):
            send_message(producer, kcfg.tx_events_topic, replayed.model_dump(mode="json"), key=user)
        # A burst: 4 distinct transactions for another user (threshold 3).
        burst_user = f"user-{uuid.uuid4().hex[:6]}"
        for i in range(4):
            send_message(producer, kcfg.tx_events_topic, _tx(burst_user, at=T0 + i).model_dump(mode="json"), key=burst_user)
        producer.flush(timeout=10)

        got = _collect_detections(kcfg, expected=2, timeout=30)
        types = {d["detection_type"] for d in got}
        assert "replay_attack" in types, got
        assert "fraud_burst" in types, got
        # Replay detection carries the originating delivery, burst carries a count.
        replay = next(d for d in got if d["detection_type"] == "replay_attack")
        assert replay["user_id"] == user
        assert replay["details"]["first_seen_record"].startswith(kcfg.tx_events_topic)
    finally:
        shutdown.set()
        engine.join(timeout=15)


def test_quarantined_user_is_blocked_end_to_end(redis_client, topics):
    kcfg = _kafka_config(topics)
    rcfg = replace(RedisConfig.from_env(), detection_cooldown_seconds=0)
    redis_client.setex(quarantine_key("bad-user"), 300, "manual-test")
    shutdown = threading.Event()
    engine = threading.Thread(target=_run_engine, args=(kcfg, rcfg, redis_client, shutdown), daemon=True)
    engine.start()
    try:
        producer = create_producer(kcfg)
        # A replayed transaction from a quarantined user: it must be refused, so
        # no replay key is ever written and no detection is produced.
        tx = _tx("bad-user")
        for _ in range(2):
            send_message(producer, kcfg.tx_events_topic, tx.model_dump(mode="json"), key="bad-user")
        producer.flush(timeout=10)

        # Read the audit topic: two transaction_blocked records, no detections.
        audit = KafkaConsumer(kcfg.audit_log_topic, bootstrap_servers=BOOTSTRAP,
                              auto_offset_reset="earliest", enable_auto_commit=False,
                              value_deserializer=lambda m: json.loads(m.decode()),
                              consumer_timeout_ms=1000, group_id=f"it-audit-{uuid.uuid4().hex[:8]}")
        blocked, deadline = [], time.time() + 25
        try:
            while time.time() < deadline and len(blocked) < 2:
                for msg in audit:
                    if msg.value.get("event") == "transaction_blocked":
                        blocked.append(msg.value)
        finally:
            audit.close()
        assert len(blocked) >= 2, blocked
        assert all(b["reason"] == "quarantine" for b in blocked)
        # The refused transactions never reached the replay detector.
        assert redis_client.keys("replay:seen:*") == []
        assert _collect_detections(kcfg, expected=1, timeout=5) == []
    finally:
        shutdown.set()
        engine.join(timeout=15)


@pytest.mark.skipif(not os.getenv("RTACE_DECISION_URL"), reason="set RTACE_DECISION_URL for the decision-service test")
def test_decision_service_allow_then_replay_deny_then_cached():
    import urllib.request

    url = os.environ["RTACE_DECISION_URL"]

    def decide(event_id, user):
        body = json.dumps({"transaction": {
            "event_id": event_id, "user_id": user, "amount": 42.5, "merchant": "Amazon",
            "timestamp": "2026-09-13T10:00:00+00:00", "location": "US-CA", "latitude": 37.0, "longitude": -122.0,
        }}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    user = f"it-{uuid.uuid4().hex[:8]}"
    first = decide(f"{user}-1", user)
    assert first["verdict"] == "ALLOW", first

    # Retrying the SAME event (the one that first recorded the request) is a
    # redelivery, not a replay: it returns the stored verdict, cached.
    again = decide(f"{user}-1", user)
    assert again["verdict"] == "ALLOW" and again.get("cached") is True, again
    assert again["decision_id"] == first["decision_id"], (first, again)

    # A DIFFERENT event id with the same payload is a replay of the request.
    replay = decide(f"{user}-2", user)
    assert replay["verdict"] == "DENY", replay
    assert any(r["type"] == "replay_attack" for r in replay["reasons"]), replay
