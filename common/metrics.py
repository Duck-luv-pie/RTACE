"""Prometheus metrics for RTACE. All components use these shared definitions."""

import time
from contextlib import contextmanager
from typing import Iterator

from prometheus_client import Counter, Histogram, REGISTRY

# Detection engine
transactions_processed_total = Counter(
    "transactions_processed_total",
    "Total transactions processed by the detection engine",
    ["status"],  # ok | replay
    registry=REGISTRY,
)
replay_detections_total = Counter(
    "replay_detections_total",
    "Total replay attacks detected",
    ["detection_type"],
    registry=REGISTRY,
)
detection_pipeline_latency_seconds = Histogram(
    "detection_pipeline_latency_seconds",
    "Time to process a transaction through the detection pipeline",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
    registry=REGISTRY,
)

# Containment engine
containment_actions_total = Counter(
    "containment_actions_total",
    "Total containment actions executed",
    ["detection_type", "action"],
    registry=REGISTRY,
)

# Redis (used by detection, containment, and API)
redis_operation_latency_seconds = Histogram(
    "redis_operation_latency_seconds",
    "Redis operation latency in seconds",
    ["operation"],
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
    registry=REGISTRY,
)


@contextmanager
def observe_redis_latency(operation: str) -> Iterator[None]:
    """Context manager to record Redis operation latency."""
    start = time.perf_counter()
    try:
        yield
    finally:
        redis_operation_latency_seconds.labels(operation=operation).observe(
            time.perf_counter() - start
        )
