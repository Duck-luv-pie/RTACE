"""Consumer loop semantics: commit-after-process, retry, DLQ, shutdown."""

import json
import threading
from dataclasses import dataclass
from typing import Optional

import redis.exceptions as redis_exc
from kafka.structs import TopicPartition

from common.consumer_loop import LoopSettings, run_consumer_loop
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


def _batch(topic, partition, values, start=0):
    tp = TopicPartition(topic, partition)
    return {
        tp: [
            Rec(topic, partition, start + i, v if isinstance(v, (bytes, type(None))) else json.dumps(v).encode())
            for i, v in enumerate(values)
        ]
    }


def _run(batches, handler, producer=None, settings=None):
    shutdown = threading.Event()
    consumer = FakeConsumer(batches, shutdown)
    settings = settings or LoopSettings(stage="test", retry_initial_seconds=0.01, retry_max_seconds=0.02)
    slept = []
    run_consumer_loop(
        consumer, handler, settings, shutdown, producer=producer, dlq_topic="dlq", sleep=slept.append
    )
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


def test_undecodable_record_is_dead_lettered_and_committed_past():
    producer = FakeProducer()
    seen = []
    consumer, _ = _run([_batch("t", 0, [b"not json", {"ok": 1}])], lambda *a: seen.append(a[3]), producer)
    assert seen == [{"ok": 1}]
    assert consumer.commits == [{TopicPartition("t", 0): 2}]
    (topic, payload, _key), = producer.sent
    assert topic == "dlq"
    assert payload["source_topic"] == "t" and payload["offset"] == 0
    assert payload["error_type"] == "ValueError"
    assert payload["value_base64"] == "bm90IGpzb24="


def test_schema_failure_is_dead_lettered():
    producer = FakeProducer()

    def handler(topic, partition, offset, value):
        DetectionEvent.model_validate(value)

    consumer, _ = _run([_batch("t", 0, [{"garbage": True}])], handler, producer)
    assert producer.sent[0][1]["error_type"] == "ValidationError"
    assert consumer.commits == [{TopicPartition("t", 0): 1}]


def test_unexpected_handler_bug_is_dead_lettered_not_retried_forever():
    producer = FakeProducer()

    def handler(*a):
        raise AttributeError("bug")

    consumer, slept = _run([_batch("t", 0, [{"x": 1}])], handler, producer)
    assert slept == []
    assert producer.sent[0][1]["error_type"] == "AttributeError"
    assert consumer.commits == [{TopicPartition("t", 0): 1}]


def test_redis_outage_blocks_and_retries_with_backoff_until_it_recovers():
    attempts = {"n": 0}

    def handler(*a):
        attempts["n"] += 1
        if attempts["n"] < 4:
            raise redis_exc.ConnectionError("redis down")

    producer = FakeProducer()
    consumer, slept = _run([_batch("t", 0, [{"x": 1}])], handler, producer)
    assert attempts["n"] == 4
    assert slept == [0.01, 0.02, 0.02]  # exponential, capped
    assert producer.sent == []  # never dead-lettered
    assert consumer.commits == [{TopicPartition("t", 0): 1}]


def test_shutdown_during_retry_leaves_offset_uncommitted():
    shutdown = threading.Event()
    consumer = FakeConsumer([_batch("t", 0, [{"x": 1}, {"x": 2}])], shutdown)
    producer = FakeProducer()

    def handler(*a):
        shutdown.set()  # operator hits Ctrl-C while Redis is down
        raise redis_exc.TimeoutError("redis slow")

    run_consumer_loop(
        consumer, handler, LoopSettings(stage="test"), shutdown, producer=producer, dlq_topic="dlq", sleep=lambda s: None
    )
    assert consumer.commits == []
    assert producer.flushed and producer.closed and consumer.closed


def test_partial_batch_progress_is_committed_when_a_later_record_hits_shutdown():
    """Records handled before the shutdown-during-retry are committed; the failing one is not."""
    shutdown = threading.Event()
    consumer = FakeConsumer([_batch("t", 0, [{"x": 1}, {"x": 2}])], shutdown)
    calls = []

    def handler(topic, partition, offset, value):
        calls.append(offset)
        if offset == 1:
            shutdown.set()
            raise redis_exc.ConnectionError("down")

    run_consumer_loop(consumer, handler, LoopSettings(stage="test"), shutdown, sleep=lambda s: None)
    assert calls == [0, 1]
    assert consumer.commits == [{TopicPartition("t", 0): 1}]


def test_tombstone_is_skipped():
    seen = []
    consumer, _ = _run([_batch("t", 0, [None, {"x": 1}])], lambda *a: seen.append(a[3]))
    assert seen == [{"x": 1}]
    assert consumer.commits == [{TopicPartition("t", 0): 2}]


def test_producer_is_flushed_and_closed_on_exit():
    producer = FakeProducer()
    _run([], lambda *a: None, producer)
    assert producer.flushed and producer.closed
