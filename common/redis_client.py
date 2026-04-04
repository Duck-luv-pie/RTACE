"""Shared Redis client for RTACE."""

import logging
from typing import Optional

import redis

from configs.redis_config import RedisConfig

logger = logging.getLogger(__name__)


def create_redis_client(config: Optional[RedisConfig] = None) -> redis.Redis:
    """Create a Redis client backed by a connection pool.

    max_connections=20 allows up to 20 simultaneous socket connections from
    this process.  Each detector can issue pipelined commands without waiting
    for another detector's round trip to complete.
    socket_keepalive keeps idle connections alive so they are immediately
    available after a quiet period without the cost of TCP re-handshake.
    """
    cfg = config or RedisConfig.from_env()
    pool = redis.ConnectionPool(
        host=cfg.host,
        port=cfg.port,
        db=cfg.db,
        decode_responses=True,
        max_connections=20,
        socket_keepalive=True,
    )
    return redis.Redis(connection_pool=pool)
