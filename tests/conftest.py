"""Shared pytest fixtures for RTACE."""

import fakeredis
import pytest

from configs.redis_config import RedisConfig


@pytest.fixture
def redis_client():
    """In-memory Redis stand-in with decode_responses=True, like the real client."""
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def redis_config() -> RedisConfig:
    """Default configuration, independent of the process environment."""
    return RedisConfig.from_env()
