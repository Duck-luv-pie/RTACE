"""Generic at-least-once consumer loop shared by the detection and containment engines.

Guarantees
- An offset is committed only after every record up to it has been handled
  (processed, or deliberately dead-lettered). Commits are synchronous and
  per poll batch, for exactly the offsets handled.
- Handlers receive the whole poll batch so they can pipeline their I/O.
  Transient failures (Redis unreachable, timed out, loading, out of memory,
  read-only) retry the whole batch with exponential backoff instead of
  skipping it; every handler is idempotent so a half-processed batch is safe
  to repeat. Detections are not silently lost while a dependency is down.
- Permanent failures (undecodable JSON, schema validation, or an unexpected
  exception) are written to the dead-letter topic with the raw payload and
  the error, counted, and committed past, so one poison record cannot wedge
  a partition. A handler reports per-record permanent failures by index; an
  unexpected exception from the handler itself dead-letters the whole batch.
- SIGINT / SIGTERM set a shutdown event: the current batch finishes (or the
  current retry is abandoned without committing), offsets are committed,
  the producer is flushed and both clients are closed.
"""

import base64
import logging
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol

import redis.exceptions as redis_exc
from kafka.errors import KafkaError
from kafka.structs import OffsetAndMetadata, TopicPartition

from common.kafka_client import decode_json, send_message
from common.metrics import consumer_records_total, dlq_messages_total, processing_errors_total

logger = logging.getLogger(__name__)

TRANSIENT_EXCEPTIONS = (
    redis_exc.ConnectionError,
    redis_exc.TimeoutError,
    redis_exc.BusyLoadingError,
    redis_exc.OutOfMemoryError,
    redis_exc.ReadOnlyError,
)


class TransientError(Exception):
    """Raise from a handler to force a retry (offsets are not committed)."""


class ShutdownRequested(Exception):
    """Raised internally when shutdown is requested while a batch is being retried."""


class Record(Protocol):
    topic: str
    partition: int
    offset: int
    key: Optional[bytes]
    value: Optional[bytes]


DecodedRecord = tuple[str, int, int, object]
"""(topic, partition, offset, decoded value)"""

BatchHandler = Callable[[list[DecodedRecord]], dict[int, Exception]]
"""handler(records) -> {index: permanent error} for records that must be dead-lettered"""

RecordHandler = Callable[[str, int, int, object], None]


def per_record(handler: RecordHandler) -> BatchHandler:
    """Adapt a one-record handler to the batch interface; each record's error is its own."""

    def _batch(records: list[DecodedRecord]) -> dict[int, Exception]:
        failures: dict[int, Exception] = {}
        for i, (topic, partition, offset, value) in enumerate(records):
            try:
                handler(topic, partition, offset, value)
            except Exception as exc:  # noqa: BLE001 - classified by the loop
                if _is_transient(exc):
                    raise
                failures[i] = exc
        return failures

    return _batch


@dataclass
class LoopSettings:
    stage: str
    poll_timeout_ms: int = 1000
    retry_initial_seconds: float = 0.5
    retry_max_seconds: float = 30.0
    kafka_error_pause_seconds: float = 1.0


def install_signal_handlers(shutdown: threading.Event) -> None:
    def _request(signum, _frame):
        logger.info("Received signal %s; shutting down after the current batch", signum)
        shutdown.set()

    signal.signal(signal.SIGINT, _request)
    signal.signal(signal.SIGTERM, _request)


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, TRANSIENT_EXCEPTIONS) or isinstance(exc, TransientError)


def run_consumer_loop(
    consumer,
    handler: BatchHandler,
    settings: LoopSettings,
    shutdown: threading.Event,
    producer=None,
    dlq_topic: Optional[str] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Poll, handle, commit, until ``shutdown`` is set. See module docstring."""
    stage = settings.stage
    logger.info("%s consumer loop started", stage)

    try:
        while not shutdown.is_set():
            try:
                batch = consumer.poll(timeout_ms=settings.poll_timeout_ms)
            except KafkaError as e:
                logger.error("%s: poll failed (%s); retrying", stage, e)
                sleep(settings.kafka_error_pause_seconds)
                continue
            if not batch:
                continue

            records: list[Record] = [r for recs in batch.values() for r in recs]
            try:
                _handle_batch_with_retry(records, handler, settings, shutdown, producer, dlq_topic, sleep)
            except ShutdownRequested:
                logger.info("%s: shutdown requested mid-retry; leaving offsets uncommitted", stage)
                break
            _commit(consumer, records, stage)
    finally:
        logger.info("%s consumer loop stopping", stage)
        if producer is not None:
            try:
                producer.flush(timeout=10)
            except Exception as e:  # noqa: BLE001 - best effort on shutdown
                logger.warning("%s: producer flush failed: %s", stage, e)
            producer.close()
        consumer.close()
        logger.info("%s consumer loop stopped", stage)


def _commit(consumer, records: list[Record], stage: str) -> None:
    offsets: dict[TopicPartition, OffsetAndMetadata] = {}
    for r in records:
        offsets[TopicPartition(r.topic, r.partition)] = OffsetAndMetadata(r.offset + 1, None, -1)
    try:
        consumer.commit(offsets=offsets)
    except KafkaError as e:
        # Records were processed; a failed commit only means they may be
        # redelivered, which every handler tolerates.
        logger.error("%s: offset commit failed (%s); records may be redelivered", stage, e)


def _handle_batch_with_retry(
    records: list[Record],
    handler: BatchHandler,
    settings: LoopSettings,
    shutdown: threading.Event,
    producer,
    dlq_topic: Optional[str],
    sleep: Callable[[float], None],
) -> None:
    stage = settings.stage

    # Decode once; undecodable or tombstone records never reach the handler.
    decoded: list[DecodedRecord] = []
    decoded_records: list[Record] = []
    for r in records:
        try:
            value = decode_json(r.value)
        except ValueError as exc:
            _permanent(r, exc, stage, producer, dlq_topic)
            continue
        if value is None:
            consumer_records_total.labels(stage=stage, result="skipped").inc()
            continue
        decoded.append((r.topic, r.partition, r.offset, value))
        decoded_records.append(r)
    if not decoded:
        return

    delay = settings.retry_initial_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            failures = handler(decoded) or {}
        except Exception as exc:  # noqa: BLE001 - classified below
            if _is_transient(exc):
                processing_errors_total.labels(stage=stage, kind="transient").inc()
                first = decoded_records[0]
                logger.warning(
                    "%s: transient failure on batch of %d starting %s[%d]@%d (attempt %d): %s; retrying in %.1fs",
                    stage, len(decoded), first.topic, first.partition, first.offset, attempt, exc, delay,
                )
                if shutdown.is_set():
                    raise ShutdownRequested() from exc
                sleep(delay)
                delay = min(delay * 2, settings.retry_max_seconds)
                if shutdown.is_set():
                    raise ShutdownRequested() from exc
                continue
            logger.exception("%s: handler raised on a batch of %d; dead-lettering all of them", stage, len(decoded))
            for r in decoded_records:
                _permanent(r, exc, stage, producer, dlq_topic)
            return

        for i, r in enumerate(decoded_records):
            if i in failures:
                logger.error("%s: permanent failure on %s[%d]@%d: %s", stage, r.topic, r.partition, r.offset, failures[i])
                _permanent(r, failures[i], stage, producer, dlq_topic)
            else:
                consumer_records_total.labels(stage=stage, result="processed").inc()
        return


def _permanent(record: Record, exc: BaseException, stage: str, producer, dlq_topic) -> None:
    processing_errors_total.labels(stage=stage, kind="permanent").inc()
    consumer_records_total.labels(stage=stage, result="dlq").inc()
    dlq_messages_total.labels(stage=stage).inc()
    if producer is None or not dlq_topic:
        logger.error("%s: no DLQ configured; dropping %s[%d]@%d", stage, record.topic, record.partition, record.offset)
        return
    payload = {
        "stage": stage,
        "source_topic": record.topic,
        "partition": record.partition,
        "offset": record.offset,
        "key": record.key.decode("utf-8", "replace") if record.key else None,
        "value_base64": base64.b64encode(record.value).decode("ascii") if record.value else None,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "failed_at": datetime.now(timezone.utc).isoformat(),
    }
    send_message(producer, dlq_topic, payload, key=payload["key"])
