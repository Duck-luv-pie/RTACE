"""Generic at-least-once consumer loop shared by the detection and containment engines.

Guarantees
- An offset is committed only after every record up to it has been handled
  (processed, or deliberately dead-lettered). Commits are synchronous and
  per batch.
- Transient failures (Redis unreachable, timed out, loading, out of memory)
  block the partition and retry with exponential backoff instead of skipping
  the record. Detections are not silently lost while a dependency is down.
- Permanent failures (undecodable JSON, schema validation, or an unexpected
  exception inside a handler) are written to the dead-letter topic with the
  raw payload and the error, counted, and then committed past, so one poison
  record cannot wedge a partition.
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
from common.metrics import (
    consumer_records_total,
    dlq_messages_total,
    processing_errors_total,
)

logger = logging.getLogger(__name__)

TRANSIENT_EXCEPTIONS = (
    redis_exc.ConnectionError,
    redis_exc.TimeoutError,
    redis_exc.BusyLoadingError,
    redis_exc.OutOfMemoryError,
    redis_exc.ReadOnlyError,
)


class TransientError(Exception):
    """Raise from a handler to force a retry (offset is not committed)."""


class ShutdownRequested(Exception):
    """Raised internally when shutdown is requested while a record is being retried."""


class Record(Protocol):
    topic: str
    partition: int
    offset: int
    key: Optional[bytes]
    value: Optional[bytes]


Handler = Callable[[str, int, int, object], None]
"""handler(topic, partition, offset, decoded_value)"""


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
    handler: Handler,
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

            to_commit: dict[TopicPartition, OffsetAndMetadata] = {}
            try:
                for tp, records in batch.items():
                    for record in records:
                        _handle_with_retry(
                            record, handler, settings, shutdown, producer, dlq_topic, sleep
                        )
                        to_commit[TopicPartition(record.topic, record.partition)] = (
                            OffsetAndMetadata(record.offset + 1, None, -1)
                        )
            except ShutdownRequested:
                logger.info("%s: shutdown requested mid-retry; leaving offsets uncommitted", stage)
            finally:
                if to_commit:
                    _commit(consumer, to_commit, stage)
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


def _commit(consumer, offsets, stage: str) -> None:
    try:
        consumer.commit(offsets=offsets)
    except KafkaError as e:
        # Records were processed; a failed commit only means they may be
        # redelivered, which every handler tolerates.
        logger.error("%s: offset commit failed (%s); records may be redelivered", stage, e)


def _handle_with_retry(
    record: Record,
    handler: Handler,
    settings: LoopSettings,
    shutdown: threading.Event,
    producer,
    dlq_topic: Optional[str],
    sleep: Callable[[float], None],
) -> None:
    stage = settings.stage
    delay = settings.retry_initial_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            value = decode_json(record.value)
            if value is None:
                consumer_records_total.labels(stage=stage, result="skipped").inc()
                return
            handler(record.topic, record.partition, record.offset, value)
            consumer_records_total.labels(stage=stage, result="processed").inc()
            return
        except Exception as exc:  # noqa: BLE001 - classified below
            if _is_transient(exc):
                processing_errors_total.labels(stage=stage, kind="transient").inc()
                logger.warning(
                    "%s: transient failure on %s[%d]@%d (attempt %d): %s; retrying in %.1fs",
                    stage, record.topic, record.partition, record.offset, attempt, exc, delay,
                )
                if shutdown.is_set():
                    raise ShutdownRequested() from exc
                sleep(delay)
                delay = min(delay * 2, settings.retry_max_seconds)
                if shutdown.is_set():
                    raise ShutdownRequested() from exc
                continue

            processing_errors_total.labels(stage=stage, kind="permanent").inc()
            logger.exception(
                "%s: permanent failure on %s[%d]@%d; dead-lettering",
                stage, record.topic, record.partition, record.offset,
            )
            _dead_letter(record, exc, stage, producer, dlq_topic)
            consumer_records_total.labels(stage=stage, result="dlq").inc()
            return


def _dead_letter(record: Record, exc: BaseException, stage: str, producer, dlq_topic) -> None:
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
