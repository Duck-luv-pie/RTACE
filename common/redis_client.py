"""Shared Redis client for RTACE."""

import logging
from typing import Optional

import redis

from configs.redis_config import RedisConfig

logger = logging.getLogger(__name__)


def create_redis_client(config: Optional[RedisConfig] = None) -> redis.Redis:
    """Create a Redis client."""
    cfg = config or RedisConfig.from_env()
    return redis.Redis(
        host=cfg.host,
        port=cfg.port,
        db=cfg.db,
        decode_responses=True,
    )
