"""Replay attack detection: the same transaction hash seen again within the replay window.

State and the check itself live in lua/tx_check.lua (one ``replay:seen:{hash}``
key per transaction, SET NX with a sliding TTL, value = identity of the
delivery that first carried it). This module turns the script's verdict into
a DetectionEvent.

Why the delivery identity matters: Kafka is at-least-once, so the same record
can arrive twice; the synchronous decision service may also have scored the
event before the Kafka record arrives. Neither is a replay. A real replay is
the same payload arriving through a *different* delivery.
"""

from datetime import datetime, timezone
from typing import Optional

from common.metrics import replay_redeliveries_total
from common.models import DetectionEvent, TransactionEvent, new_detection_id

REDIS_KEY_PREFIX = "replay:seen:"


def replay_key(tx: TransactionEvent) -> str:
    return f"{REDIS_KEY_PREFIX}{tx.replay_hash()}"


def interpret(tx: TransactionEvent, status: str, stored_ref: str) -> Optional[DetectionEvent]:
    if status == "redelivery":
        replay_redeliveries_total.inc()
        return None
    if status != "replay":
        return None
    return DetectionEvent(
        detection_id=new_detection_id("replay"),
        detection_type="replay_attack",
        severity="high",
        user_id=tx.user_id,
        transaction_id=tx.event_id,
        timestamp=datetime.now(timezone.utc),
        details={"first_seen_record": stored_ref},
    )
