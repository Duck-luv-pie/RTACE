"""Fraud burst detection: too many transactions from a user in a rolling time window."""

from datetime import datetime, timezone
from typing import Optional

from common.metrics import observe_redis_latency
from common.models import DetectionEvent, TransactionEvent, new_detection_id
from configs.redis_config import RedisConfig

BURST_KEY_PREFIX = "burst:"
BURST_KEY_SUFFIX = ":1m"


def _burst_key(user_id: str) -> str:
    return f"{BURST_KEY_PREFIX}{user_id}{BURST_KEY_SUFFIX}"


def check_fraud_burst(
    tx: TransactionEvent,
    redis_client,
    redis_config: Optional[RedisConfig] = None,
) -> Optional[DetectionEvent]:
    """
    Track transactions per user in a Redis sorted set (member = event_id,
    score = unix timestamp). Trim entries older than the rolling window, add
    this transaction, and emit fraud_burst if the count exceeds the threshold.
    Using event_id as the member makes redelivery of the same record idempotent.
    """
    config = redis_config or RedisConfig.from_env()
    key = _burst_key(tx.user_id)
    now_ts = tx.timestamp.timestamp()
    cutoff = now_ts - float(config.burst_window_seconds)

    with observe_redis_latency("burst_window"):
        with redis_client.pipeline(transaction=False) as pipe:
            pipe.zremrangebyscore(key, "-inf", cutoff)
            pipe.zadd(key, {tx.event_id: now_ts})
            pipe.zcard(key)
            pipe.expire(key, config.burst_key_ttl_seconds)
            _, _, count, _ = pipe.execute()

    if count > config.burst_threshold:
        return DetectionEvent(
            detection_id=new_detection_id("burst"),
            detection_type="fraud_burst",
            severity="high",
            user_id=tx.user_id,
            transaction_id=tx.event_id,
            timestamp=datetime.now(timezone.utc),
            details={
                "count_in_window": count,
                "window_seconds": config.burst_window_seconds,
                "threshold": config.burst_threshold,
            },
        )
    return None
