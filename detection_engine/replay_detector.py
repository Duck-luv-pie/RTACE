"""Replay attack detection: the same transaction hash seen again within the replay window."""

from datetime import datetime, timezone
from typing import Optional

from common.metrics import observe_redis_latency, replay_redeliveries_total
from common.models import DetectionEvent, TransactionEvent, new_detection_id
from configs.redis_config import RedisConfig

REDIS_KEY_PREFIX = "replay:seen:"


def _key(tx_hash: str) -> str:
    return f"{REDIS_KEY_PREFIX}{tx_hash}"


def check_replay(
    tx: TransactionEvent,
    redis_client,
    redis_config: Optional[RedisConfig] = None,
    record_ref: Optional[str] = None,
) -> Optional[DetectionEvent]:
    """Return a replay_attack detection if this exact transaction was already seen.

    State: one Redis key per transaction hash, ``replay:seen:{hash}``, set with
    ``NX`` and a sliding TTL of ``replay_ttl_hours``. A per-key TTL has no
    day-boundary hole (a replay at 00:01 of something first seen at 23:59 is
    still caught) and does not get its expiry refreshed by unrelated writes.

    The key's value is ``record_ref``: an identifier of the Kafka record that
    first carried this transaction (``topic:partition:offset``). Kafka is
    at-least-once, so after a crash the same record can be delivered again. A
    redelivered record hits an existing key whose value equals its own
    ``record_ref`` and is *not* a replay; a real replay arrives in a different
    record and its ``record_ref`` differs. Callers without Kafka coordinates may
    pass ``None``, in which case every repeat counts as a replay.
    """
    config = redis_config or RedisConfig.from_env()
    key = _key(tx.replay_hash())
    ttl_seconds = config.replay_ttl_hours * 3600
    value = record_ref or tx.event_id

    with observe_redis_latency("replay_check"):
        first_seen = redis_client.set(key, value, nx=True, ex=ttl_seconds)
        if first_seen:
            return None
        stored = redis_client.get(key)

    if record_ref is not None and stored == record_ref:
        replay_redeliveries_total.inc()
        return None

    return DetectionEvent(
        detection_id=new_detection_id("replay"),
        detection_type="replay_attack",
        severity="high",
        user_id=tx.user_id,
        transaction_id=tx.event_id,
        timestamp=datetime.now(timezone.utc),
        details={"first_seen_record": stored},
    )
