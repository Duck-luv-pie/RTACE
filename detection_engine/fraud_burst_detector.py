"""Fraud burst detection: too many transactions from a user in a rolling time window.

lua/tx_check.lua maintains the per-user sorted set ``burst:{user_id}:1m``
(member = event_id so redelivery is idempotent, score = event time), trims
entries older than the window, adds this transaction and returns the count.
"""

from datetime import datetime, timezone
from typing import Optional

from common.models import DetectionEvent, TransactionEvent, new_detection_id
from configs.redis_config import RedisConfig

BURST_KEY_PREFIX = "burst:"
BURST_KEY_SUFFIX = ":1m"


def burst_key(user_id: str) -> str:
    return f"{BURST_KEY_PREFIX}{user_id}{BURST_KEY_SUFFIX}"


def interpret(tx: TransactionEvent, count: int, config: RedisConfig) -> Optional[DetectionEvent]:
    if count <= config.burst_threshold:
        return None
    return DetectionEvent(
        detection_id=new_detection_id("burst"),
        detection_type="fraud_burst",
        severity="high",
        user_id=tx.user_id,
        transaction_id=tx.event_id,
        timestamp=datetime.now(timezone.utc),
        details={
            "count_in_window": int(count),
            "window_seconds": config.burst_window_seconds,
            "threshold": config.burst_threshold,
        },
    )
