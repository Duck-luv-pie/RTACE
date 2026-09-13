"""Generic at-least-once consumer loop shared by the detection and containment engines.

Guarantees
- End-to-end at-least-once. An offset is committed only after every record up
  to it has been handled (processed, or deliberately dead-lettered) AND every
  message that handling produced (detections, audit records, DLQ records) has
  been acknowledged by the broker. The producer is flushed and verified before
  the commit, so a crash between handling a record and delivering its detection
  cannot commit the input offset with the detection lost; the record is
  redelivered instead. Commits are synchronous and per poll batch, for exactly
  the offsets handled.
- Handlers receive the whole poll batch so they can pipeline their I/O.
  Transient failures (Redis unreachable, timed out, loading, out of memory,
  read-only) and produce/delivery failures retry the whole batch with
  exponential backoff instead of skipping it; every handler and every
  downstream write is idempotent so a half-processed batch is safe to repeat.
  Detections are not silently lost while a dependency is down.
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

from common.kafka_client import decode_json, flush_and_verify, reset_send_errors, send_message
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
    # KafkaError here means a produce/flush failure (see flush_and_verify): the
    # batch must be retried, not committed, so detections are not lost.
    return isinstance(exc, TRANSIENT_EXCEPTIONS) or isinstance(exc, (TransientError, KafkaError))


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

    # Decode once; undecodable records are dead-lettered, tombstones skipped.
    # These never change across retries, so classify them before the loop.
    decoded: list[DecodedRecord] = []
    decoded_records: list[Record] = []
    predecode_dlq: list[tuple[Record, Exception]] = []
    for r in records:
        try:
            value = decode_json(r.value)
        except ValueError as exc:
            predecode_dlq.append((r, exc))
            continue
        if value is None:
            consumer_records_total.labels(stage=stage, result="skipped").inc()
            continue
        decoded.append((r.topic, r.partition, r.offset, value))
        decoded_records.append(r)
    if not decoded and not predecode_dlq:
        return

    delay = settings.retry_initial_seconds
    attempt = 0
    while True:
        attempt += 1
        # A fresh batch of async sends; forget any error latched by a prior attempt.
        if producer is not None:
            reset_send_errors(producer)

        # Classify this attempt: which records are processed, which are poison.
        dlq: list[tuple[Record, BaseException]] = list(predecode_dlq)
        processed: list[Record] = []
        try:
            failures = handler(decoded) if decoded else {}
        except Exception as exc:  # noqa: BLE001 - classified here
            if _is_transient(exc):
                if _backoff(stage, decoded_records or [r for r, _ in predecode_dlq],
                            attempt, delay, exc, shutdown, sleep):
                    delay = min(delay * 2, settings.retry_max_seconds)
                    continue
            # A non-transient handler bug dead-letters the whole decoded batch.
            logger.exception("%s: handler raised on a batch of %d; dead-lettering all", stage, len(decoded))
            dlq += [(r, exc) for r in decoded_records]
        else:
            for i, r in enumerate(decoded_records):
                if i in failures:
                    logger.error("%s: permanent failure on %s[%d]@%d: %s",
                                 stage, r.topic, r.partition, r.offset, failures[i])
                    dlq.append((r, failures[i]))
                else:
                    processed.append(r)

        # Produce every DLQ record this attempt found.
        for r, exc in dlq:
            _send_to_dlq(r, exc, stage, producer, dlq_topic)

        # Durability gate: block until every detection, audit and DLQ record this
        # attempt produced is acknowledged by the broker. Only after this may the
        # caller commit input offsets, so a crash cannot lose a detection whose
        # input offset was committed. A delivery failure raises KafkaError; retry
        # the batch (every downstream write is idempotent) rather than commit.
        if producer is not None:
            try:
                flush_and_verify(producer)
            except KafkaError as exc:
                if _backoff(stage, decoded_records or [r for r, _ in predecode_dlq],
                            attempt, delay, exc, shutdown, sleep):
                    delay = min(delay * 2, settings.retry_max_seconds)
                    continue

        # Delivery is durable: count each record exactly once, then return to commit.
        for _ in processed:
            consumer_records_total.labels(stage=stage, result="processed").inc()
        for _r, _exc in dlq:
            processing_errors_total.labels(stage=stage, kind="permanent").inc()
            dlq_messages_total.labels(stage=stage).inc()
            consumer_records_total.labels(stage=stage, result="dlq").inc()
        return


def _backoff(
    stage: str,
    records: list[Record],
    attempt: int,
    delay: float,
    exc: BaseException,
    shutdown: threading.Event,
    sleep: Callable[[float], None],
) -> bool:
    """Record a transient failure, sleep, and signal that the batch should retry.

    Raises ShutdownRequested if shutdown was requested (so offsets stay
    uncommitted). Always returns True otherwise.
    """
    processing_errors_total.labels(stage=stage, kind="transient").inc()
    first = records[0]
    logger.warning(
        "%s: transient failure on batch of %d starting %s[%d]@%d (attempt %d): %s; retrying in %.1fs",
        stage, len(records), first.topic, first.partition, first.offset, attempt, exc, delay,
    )
    if shutdown.is_set():
        raise ShutdownRequested() from exc
    sleep(delay)
    if shutdown.is_set():
        raise ShutdownRequested() from exc
    return True


def _send_to_dlq(record: Record, exc: BaseException, stage: str, producer, dlq_topic) -> None:
    """Produce one dead-letter record (async). Terminal counters are incremented
    once by the caller after the batch is durably delivered, so a retry does not
    inflate them."""
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
