"""Shared pytest fixtures for RTACE."""

import os

import fakeredis
import pytest
import redis

from configs.kafka_config import KafkaConfig
from configs.redis_config import RedisConfig
from detection_engine.pipeline import DetectionPipeline

_REAL_REDIS_URL = os.getenv("RTACE_TEST_REDIS_URL")  # e.g. redis://localhost:6379/15


@pytest.fixture
def redis_client():
    """A Redis to run against.

    Default: fakeredis with Lua support (no infrastructure). Set
    RTACE_TEST_REDIS_URL to run the same tests against a real Redis, which
    exercises the real Lua interpreter; the chosen DB is flushed per test.
    """
    if _REAL_REDIS_URL:
        r = redis.Redis.from_url(_REAL_REDIS_URL, decode_responses=True)
        r.flushdb()
        yield r
        r.flushdb()
    else:
        yield fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def redis_config() -> RedisConfig:
    """Default configuration, independent of the process environment."""
    return RedisConfig.from_env()


@pytest.fixture
def kafka_config() -> KafkaConfig:
    return KafkaConfig.from_env()


@pytest.fixture
def make_pipeline(redis_client, redis_config, kafka_config):
    """Factory: make_pipeline(**config_overrides) -> (pipeline, emitted list, audits list)."""
    from dataclasses import replace

    def _make(**overrides):
        out, audits = [], []
        p = DetectionPipeline(redis_client, out.append, kafka_config, replace(redis_config, **overrides), emit_audit=audits.append)
        return p, out, audits

    return _make
