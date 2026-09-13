"""Consumer loop semantics: commit-after-process, batch retry, DLQ, shutdown."""

import json
import threading
from dataclasses import dataclass
from typing import Optional

import redis.exceptions as redis_exc
from kafka.errors import KafkaError
from kafka.structs import TopicPartition

from common.consumer_loop import LoopSettings, per_record, run_consumer_loop
from common.kafka_client import send_message
from common.models import DetectionEvent


@dataclass
class Rec:
    topic: str
    partition: int
    offset: int
    value: Optional[bytes]
    key: Optional[bytes] = None


class FakeConsumer:
    """Serves pre-baked poll batches, records commits, then triggers shutdown."""

    def __init__(self, batches, shutdown):
        self.batches = list(batches)
        self.shutdown = shutdown
        self.commits = []
        self.closed = False

    def poll(self, timeout_ms=0):
        if not self.batches:
            self.shutdown.set()
            return {}
        return self.batches.pop(0)

    def commit(self, offsets=None):
        self.commits.append({tp: om.offset for tp, om in offsets.items()})

    def close(self):
        self.closed = True


class FakeProducer:
    def __init__(self):
        self.sent = []
        self.flushed = False
        self.closed = False

    def send(self, topic, value=None, key=None):
        self.sent.append((topic, value, key))

        class _F:
            def add_errback(self, cb):
                return self

        return _F()

    def flush(self, timeout=None):
        self.flushed = True

    def close(self):
        self.closed = True


class FailingProducer(FakeProducer):
    """Mimics the real producer's errback contract: the first ``fail_attempts``
    flushes worth of sends report a delivery failure (latched on the producer,
    as common.kafka_client.send_message does), then sends succeed."""

    def __init__(self, fail_attempts=1):
        super().__init__()
        self.fail_attempts = fail_attempts
        self.flushes = 0

    def send(self, topic, value=None, key=None):
        self.sent.append((topic, value, key))
        outer = self

        class _F:
            def add_errback(self, cb):
                if outer.fail_attempts > 0:
                    cb(KafkaError("broker unavailable"))  # sets producer._rtace_last_error
                return self

        return _F()

    def flush(self, timeout=None):
        self.flushed = True
        self.flushes += 1
        if self.fail_attempts > 0:
            self.fail_attempts -= 1


def _batch(topic, partition, values, start=0):
    tp = TopicPartition(topic, partition)
    return {tp: [Rec(topic, partition, start + i, v if isinstance(v, (bytes, type(None))) else json.dumps(v).encode())
                 for i, v in enumerate(values)]}


def _run(batches, handler, producer=None, settings=None, batch_handler=None):
    shutdown = threading.Event()
    consumer = FakeConsumer(batches, shutdown)
    settings = settings or LoopSettings(stage="test", retry_initial_seconds=0.01, retry_max_seconds=0.02)
    slept = []
    run_consumer_loop(consumer, batch_handler or per_record(handler), settings, shutdown,
                      producer=producer, dlq_topic="dlq", sleep=slept.append)
    return consumer, slept


def test_processes_every_record_and_commits_next_offset():
    seen = []
    consumer, _ = _run([_batch("t", 0, [{"a": 1}, {"a": 2}], start=10)], lambda *a: seen.append(a[3]))
    assert seen == [{"a": 1}, {"a": 2}]
    assert consumer.commits == [{TopicPartition("t", 0): 12}]
    assert consumer.closed


def test_commit_covers_each_partition_in_the_batch():
    batch = {**_batch("t", 0, [{"a": 1}], start=5), **_batch("t", 1, [{"a": 2}, {"a": 3}], start=7)}
    consumer, _ = _run([batch], lambda *a: None)
    assert consumer.commits == [{TopicPartition("t", 0): 6, TopicPartition("t", 1): 9}]


def test_batch_handler_receives_the_whole_poll_batch():
    sizes = []

    def handler(records):
        sizes.append(len(records))
        return {}

    batch = {**_batch("t", 0, [{"a": 1}, {"a": 2}]), **_batch("t", 1, [{"a": 3}])}
    consumer, _ = _run([batch], None, batch_handler=handler)
    assert sizes == [3]
    assert consumer.commits == [{TopicPartition("t", 0): 2, TopicPartition("t", 1): 1}]


def test_undecodable_record_is_dead_lettered_and_committed_past():
    producer = FakeProducer()
    seen = []
    consumer, _ = _run([_batch("t", 0, [b"not json", {"ok": 1}])], lambda *a: seen.append(a[3]), producer)
    assert seen == [{"ok": 1}]
    assert consumer.commits == [{TopicPartition("t", 0): 2}]
    (topic, payload, _key), = producer.sent
    assert topic == "dlq" and payload["source_topic"] == "t" and payload["offset"] == 0
    assert payload["error_type"] == "ValueError" and payload["value_base64"] == "bm90IGpzb24="


def test_schema_failure_is_dead_lettered():
    producer = FakeProducer()

    def handler(topic, partition, offset, value):
        DetectionEvent.model_validate(value)

    consumer, _ = _run([_batch("t", 0, [{"garbage": True}])], handler, producer)
    assert producer.sent[0][1]["error_type"] == "ValidationError"
    assert consumer.commits == [{TopicPartition("t", 0): 1}]


def test_batch_handler_per_index_failures_dead_letter_only_those():
    producer = FakeProducer()

    def handler(records):
        return {1: ValueError("bad one")}

    consumer, _ = _run([_batch("t", 0, [{"a": 1}, {"a": 2}, {"a": 3}])], None, producer, batch_handler=handler)
    assert [p["offset"] for _, p, _ in producer.sent] == [1]
    assert consumer.commits == [{TopicPartition("t", 0): 3}]


def test_unexpected_handler_bug_is_dead_lettered_not_retried_forever():
    producer = FakeProducer()

    def handler(*a):
        raise AttributeError("bug")

    consumer, slept = _run([_batch("t", 0, [{"x": 1}])], handler, producer)
    assert slept == []
    assert producer.sent[0][1]["error_type"] == "AttributeError"
    assert consumer.commits == [{TopicPartition("t", 0): 1}]


def test_redis_outage_blocks_and_retries_the_batch_with_backoff():
    attempts = {"n": 0}

    def handler(*a):
        attempts["n"] += 1
        if attempts["n"] < 4:
            raise redis_exc.ConnectionError("redis down")

    producer = FakeProducer()
    consumer, slept = _run([_batch("t", 0, [{"x": 1}])], handler, producer)
    assert attempts["n"] == 4
    assert slept == [0.01, 0.02, 0.02]
    assert producer.sent == []
    assert consumer.commits == [{TopicPartition("t", 0): 1}]


def test_shutdown_during_retry_leaves_offsets_uncommitted():
    shutdown = threading.Event()
    consumer = FakeConsumer([_batch("t", 0, [{"x": 1}, {"x": 2}])], shutdown)
    producer = FakeProducer()

    def handler(*a):
        shutdown.set()
        raise redis_exc.TimeoutError("redis slow")

    run_consumer_loop(consumer, per_record(handler), LoopSettings(stage="test"), shutdown,
                      producer=producer, dlq_topic="dlq", sleep=lambda s: None)
    assert consumer.commits == []
    assert producer.flushed and producer.closed and consumer.closed


def test_tombstone_is_skipped():
    seen = []
    consumer, _ = _run([_batch("t", 0, [None, {"x": 1}])], lambda *a: seen.append(a[3]))
    assert seen == [{"x": 1}]
    assert consumer.commits == [{TopicPartition("t", 0): 2}]


def test_producer_is_flushed_and_closed_on_exit():
    producer = FakeProducer()
    _run([], lambda *a: None, producer)
    assert producer.flushed and producer.closed


def _emit_handler(producer, topic="detections"):
    """A batch handler that produces one downstream message per record."""

    def handler(records):
        for _t, _p, _o, value in records:
            send_message(producer, topic, {"echo": value})
        return {}

    return handler


def test_failed_detection_send_blocks_commit_until_redelivered():
    """The offset must not be committed while a detection this batch produced is
    not yet acknowledged by the broker; the batch retries and only commits once
    delivery succeeds."""
    producer = FailingProducer(fail_attempts=1)
    consumer, slept = _run([_batch("t", 0, [{"a": 1}])], None, producer, batch_handler=_emit_handler(producer))
    assert consumer.commits == [{TopicPartition("t", 0): 1}]  # committed exactly once, after success
    assert producer.flushes >= 2  # first flush failed, a later one succeeded (plus the exit flush)
    assert slept == [0.01]        # exactly one backoff between the two attempts


def test_successful_batch_flushes_before_committing():
    """Ordering guarantee: every batch is flushed (delivery confirmed) before commit."""
    producer = FakeProducer()
    consumer, _ = _run([_batch("t", 0, [{"a": 1}, {"a": 2}])], None, producer, batch_handler=_emit_handler(producer))
    assert producer.flushed is True
    assert consumer.commits == [{TopicPartition("t", 0): 2}]
    assert len(producer.sent) == 2  # both detections produced


def test_persistent_send_failure_never_commits():
    """If delivery keeps failing, the offset is never committed (no data loss);
    the batch keeps retrying until shutdown."""
    producer = FailingProducer(fail_attempts=10_000)
    shutdown = threading.Event()
    consumer = FakeConsumer([_batch("t", 0, [{"a": 1}])], shutdown)
    attempts = {"n": 0}

    def handler(records):
        attempts["n"] += 1
        if attempts["n"] >= 3:
            shutdown.set()  # give up after a few tries, like an operator stopping the worker
        for _t, _p, _o, value in records:
            send_message(producer, "detections", {"echo": value})
        return {}

    run_consumer_loop(consumer, handler, LoopSettings(stage="test", retry_initial_seconds=0.001),
                      shutdown, producer=producer, dlq_topic="dlq", sleep=lambda s: None)
    assert consumer.commits == []  # never committed a batch whose detections were lost
    assert attempts["n"] >= 3
