"""Detection pipeline: turns a batch of Kafka records into DetectionEvents.

Per poll batch (up to max_poll_records records) the pipeline makes a fixed,
small number of Redis round trips instead of ~4 per record:

  A. enforcement   one pipeline of EXISTS   (quarantine per tx, IP block per login)
  B. checks        one pipeline of Lua calls (tx_check.lua / auth_check.lua)
  C. pre-scored    one pipeline of GETs      (only if the decision service saw some)
  D. cooldown      one pipeline of SET NX    (only if there are candidate detections)

Records that fail validation are reported back per index so the consumer loop
can dead-letter them without failing the batch; Redis errors raise and the
loop retries the whole batch (every step is idempotent).
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional, Union

from pydantic import ValidationError

from common.enforcement import ip_block_key, quarantine_key
from common.metrics import (
    credential_stuffing_detections_total,
    detection_batch_size,
    events_blocked_total,
    fraud_burst_detections_total,
    geo_velocity_detections_total,
    observe_redis_latency,
    replay_detections_total,
    transactions_processed_total,
)
from common.models import AuthEvent, DetectionEvent, TransactionEvent, stable_detection_id
from configs.kafka_config import KafkaConfig
from configs.redis_config import RedisConfig
from detection_engine import (
    credential_stuffing_detector,
    fraud_burst_detector,
    geo_velocity_detector,
    replay_detector,
)
from detection_engine.cooldown import filter_cooled_down, release_cooldown
from detection_engine.scripts import (
    TX_BURST_COUNT,
    TX_REPLAY_STATUS,
    TX_STORED_REF,
    Scripts,
)

logger = logging.getLogger(__name__)

Emit = Callable[[DetectionEvent], None]
EmitAudit = Callable[[dict], None]
Record = tuple[str, int, int, dict]  # (topic, partition, offset, decoded value)

DECISION_RESULT_PREFIX = "decision:result:"


def record_ref(topic: str, partition: int, offset: int) -> str:
    """Identity of a Kafka delivery; stable across redeliveries of the same record."""
    return f"{topic}:{partition}:{offset}"


@dataclass
class _Item:
    index: int
    event: Union[TransactionEvent, AuthEvent]
    ref: str
    blocked: bool = False
    result: Optional[list] = None
    candidates: list[DetectionEvent] = field(default_factory=list)

    @property
    def is_tx(self) -> bool:
        return isinstance(self.event, TransactionEvent)


@dataclass
class BatchResult:
    emitted: list[DetectionEvent]
    failures: dict[int, Exception]  # record index -> permanent error
    claimed_cooldown_keys: list[str] = field(default_factory=list)


# Detector type -> id prefix (scope appended for credential stuffing).
_ID_PREFIX = {
    "replay_attack": "replay",
    "geo_velocity_anomaly": "geo",
    "fraud_burst": "burst",
    "credential_stuffing": "cred",
}


def _assign_stable_id(detection: DetectionEvent, ref: str) -> None:
    """Give the detection a deterministic id for this (delivery, detector, scope),
    so a redelivery or a reprocessed batch produces the same id and containment
    applies it exactly once."""
    scope = detection.details.get("scope", "")
    prefix = _ID_PREFIX.get(detection.detection_type, "det")
    if detection.detection_type == "credential_stuffing" and scope:
        prefix = f"{prefix}-{scope}"
    detection.detection_id = stable_detection_id(prefix, ref, detection.detection_type, scope)


class DetectionPipeline:
    def __init__(
        self,
        redis_client,
        emit: Emit,
        kafka_config: Optional[KafkaConfig] = None,
        redis_config: Optional[RedisConfig] = None,
        emit_audit: Optional[EmitAudit] = None,
    ) -> None:
        self.redis = redis_client
        self.emit = emit
        self.emit_audit = emit_audit
        self.kafka_config = kafka_config or KafkaConfig.from_env()
        # Loaded once; never re-read from the environment on the hot path.
        self.redis_config = redis_config or RedisConfig.from_env()
        self.scripts = Scripts(redis_client)

    # ---------------------------------------------------------------- entry

    def handle_record(self, topic: str, partition: int, offset: int, value: dict) -> list[DetectionEvent]:
        """Single-record convenience: raises the record's permanent error, if any."""
        result = self.handle_batch([(topic, partition, offset, value)])
        if result.failures:
            raise result.failures[0]
        return result.emitted

    def handle_transaction(self, tx: TransactionEvent, ref: Optional[str] = None) -> list[DetectionEvent]:
        """Without a delivery ref every call counts as a distinct delivery."""
        return self._run([_Item(0, tx, ref or f"direct:{uuid.uuid4()}")]).emitted

    def handle_auth(self, auth: AuthEvent) -> list[DetectionEvent]:
        return self._run([_Item(0, auth, f"direct-auth:{auth.event_id}")]).emitted

    def handle_batch(self, records: list[Record]) -> BatchResult:
        detection_batch_size.observe(len(records))
        items: list[_Item] = []
        failures: dict[int, Exception] = {}
        for i, (topic, partition, offset, value) in enumerate(records):
            try:
                if topic == self.kafka_config.auth_events_topic:
                    items.append(_Item(i, AuthEvent.model_validate(value), record_ref(topic, partition, offset)))
                else:
                    items.append(_Item(i, TransactionEvent.model_validate(value), record_ref(topic, partition, offset)))
            except ValidationError as e:
                failures[i] = e
        result = self._run(items)
        result.failures.update(failures)
        return result

    # ------------------------------------------------------------- phases

    def _run(self, items: list[_Item]) -> BatchResult:
        if not items:
            return BatchResult([], {})
        cfg = self.redis_config

        # A. enforcement -------------------------------------------------
        with observe_redis_latency("enforce_batch"):
            with self.redis.pipeline(transaction=False) as pipe:
                for it in items:
                    pipe.exists(quarantine_key(it.event.user_id) if it.is_tx else ip_block_key(it.event.ip_address))
                flags = pipe.execute()
        for it, flag in zip(items, flags):
            if flag:
                it.blocked = True
                self._blocked(it)

        # B. checks --------------------------------------------------------
        active = [it for it in items if not it.blocked and (it.is_tx or not it.event.success)]
        if active:
            with observe_redis_latency("detect_batch"):
                with self.redis.pipeline(transaction=False) as pipe:
                    for it in active:
                        if it.is_tx:
                            self._queue_tx_check(pipe, it.event, it.ref)
                        else:
                            self._queue_auth_check(pipe, it.event)
                    results = pipe.execute()
            for it, res in zip(active, results):
                it.result = res

        # interpret ---------------------------------------------------------
        prescored: list[_Item] = []
        for it in active:
            if it.is_tx:
                tx, res = it.event, it.result
                status = res[TX_REPLAY_STATUS]
                if status == "prescored":
                    prescored.append(it)
                    continue
                for d in (
                    replay_detector.interpret(tx, status, res[TX_STORED_REF]),
                    geo_velocity_detector.interpret(tx, res, cfg),
                    fraud_burst_detector.interpret(tx, int(res[TX_BURST_COUNT]), cfg),
                ):
                    if d is not None:
                        it.candidates.append(d)
                transactions_processed_total.labels(outcome="detected" if it.candidates else "clean").inc()
            else:
                cu, ci = it.result
                it.candidates.extend(d for d in credential_stuffing_detector.interpret(it.event, int(cu), int(ci), cfg) if d)

        # C. pre-scored by the decision service ------------------------------
        if prescored:
            with observe_redis_latency("decision_fetch"):
                with self.redis.pipeline(transaction=False) as pipe:
                    for it in prescored:
                        pipe.get(f"{DECISION_RESULT_PREFIX}{it.event.event_id}")
                    payloads = pipe.execute()
            for it, raw in zip(prescored, payloads):
                it.candidates.extend(self._detections_from_decision(it.event, raw))
                transactions_processed_total.labels(outcome="prescored").inc()

        # Stable ids: derive from the delivery ref so a redelivery or a retried
        # batch reproduces the same id and containment applies it exactly once.
        for it in items:
            for d in it.candidates:
                _assign_stable_id(d, it.ref)

        # D. cooldown + emit ---------------------------------------------------
        candidates = [d for it in items for d in it.candidates]
        for d in candidates:
            self._count(d)
        emitted, claimed = filter_cooled_down(candidates, self.redis, cfg)
        for d in emitted:
            self.emit(d)
            logger.info(
                "%s: user_id=%s transaction_id=%s detection_id=%s details=%s",
                d.detection_type, d.user_id, d.transaction_id, d.detection_id, d.details,
            )
        return BatchResult(emitted, {}, claimed)

    def release_cooldown(self, keys: list[str]) -> None:
        """Undo cooldown reservations for a batch whose detections were not
        delivered, so the retry can re-emit them (see the durability gate in
        common.consumer_loop)."""
        release_cooldown(keys, self.redis)

    # ------------------------------------------------------------ helpers

    def _queue_tx_check(self, pipe, tx: TransactionEvent, ref: str) -> None:
        cfg = self.redis_config
        now_ts = tx.timestamp.timestamp()
        self.scripts.tx_check(
            keys=[replay_detector.replay_key(tx), fraud_burst_detector.burst_key(tx.user_id),
                  geo_velocity_detector.session_key(tx.user_id)],
            args=[ref, cfg.replay_ttl_hours * 3600, tx.event_id, repr(now_ts),
                  repr(now_ts - cfg.burst_window_seconds), cfg.burst_key_ttl_seconds,
                  repr(tx.latitude), repr(tx.longitude), cfg.session_ttl_days * 86400,
                  repr(cfg.geo_max_velocity_kmh), repr(geo_velocity_detector.MIN_TIME_SECONDS)],
            client=pipe,
        )

    def _queue_auth_check(self, pipe, auth: AuthEvent) -> None:
        cfg = self.redis_config
        now_ts = auth.timestamp.timestamp()
        self.scripts.auth_check(
            keys=[credential_stuffing_detector.user_key(auth), credential_stuffing_detector.ip_key(auth)],
            args=[auth.event_id, repr(now_ts), repr(now_ts - cfg.auth_fail_window_seconds), cfg.auth_fail_key_ttl_seconds],
            client=pipe,
        )

    @staticmethod
    def _detections_from_decision(tx: TransactionEvent, raw: Optional[str]) -> list[DetectionEvent]:
        """The decision service scored this event synchronously and left its findings
        in Redis; re-emit them so containment and the audit log stay consistent."""
        if not raw:
            return []
        try:
            decision = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Unreadable decision result for event %s", tx.event_id)
            return []
        out = []
        for reason in decision.get("reasons", []):
            dtype = reason.get("type")
            if dtype not in ("replay_attack", "geo_velocity_anomaly", "fraud_burst"):
                continue
            out.append(DetectionEvent(
                # Placeholder; _run reassigns a stable id from the delivery ref.
                detection_id="pending",
                detection_type=dtype,
                severity="high",
                user_id=tx.user_id,
                transaction_id=tx.event_id,
                timestamp=tx.timestamp,
                details={**reason.get("details", {}), "source": "decision-service", "verdict": decision.get("verdict")},
            ))
        return out

    def _blocked(self, it: _Item) -> None:
        """An active rule rejects this event: it is not analysed, it is refused.

        A quarantined user's transactions must not advance their trusted
        location or burst window, and a blocked IP's login attempts must not
        keep feeding the stuffing counters."""
        ev = it.event
        event_type, reason = ("transaction", "quarantine") if it.is_tx else ("auth", "ip_block")
        events_blocked_total.labels(event_type=event_type, reason=reason).inc()
        if it.is_tx:
            transactions_processed_total.labels(outcome="blocked").inc()
        logger.info("Blocked %s: user_id=%s event_id=%s reason=%s", event_type, ev.user_id, ev.event_id, reason)
        if self.emit_audit is not None:
            self.emit_audit({
                "event": f"{event_type}_blocked", "reason": reason, "user_id": ev.user_id,
                "event_id": ev.event_id, "ip_address": getattr(ev, "ip_address", None),
            })

    @staticmethod
    def _count(detection: DetectionEvent) -> None:
        dtype = detection.detection_type
        if dtype == "replay_attack":
            replay_detections_total.labels(detection_type=dtype).inc()
        elif dtype == "geo_velocity_anomaly":
            geo_velocity_detections_total.labels(detection_type=dtype).inc()
        elif dtype == "fraud_burst":
            fraud_burst_detections_total.labels(detection_type=dtype).inc()
        elif dtype == "credential_stuffing":
            credential_stuffing_detections_total.labels(scope=detection.details.get("scope", "user")).inc()
