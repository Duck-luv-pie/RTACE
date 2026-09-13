"""Credential stuffing detection: failed login bursts per user or per IP (rolling window)."""

from datetime import datetime, timezone
from typing import Optional, Tuple

from common.metrics import observe_redis_latency
from common.models import AuthEvent, DetectionEvent, new_detection_id
from configs.redis_config import RedisConfig

USER_KEY_PREFIX = "auth:fail:user:"
IP_KEY_PREFIX = "auth:fail:ip:"
KEY_SUFFIX = ":1m"


def sanitize_ip_for_key(ip: str) -> str:
    """Avoid ambiguous colons in Redis keys for IPv6."""
    return ip.replace(":", "-")


def _window_counts(
    redis_client,
    keys: list[str],
    member: str,
    now_ts: float,
    window_seconds: int,
    key_ttl_seconds: int,
) -> list[int]:
    """For each key: trim old members, add the attempt, refresh TTL; return counts.

    Everything is pipelined so the whole check costs a single Redis round trip
    regardless of how many windows are tracked.
    """
    cutoff = now_ts - float(window_seconds)
    with observe_redis_latency("credential_stuffing_window"):
        with redis_client.pipeline(transaction=False) as pipe:
            for key in keys:
                pipe.zremrangebyscore(key, "-inf", cutoff)
                pipe.zadd(key, {member: now_ts})
                pipe.zcard(key)
                pipe.expire(key, key_ttl_seconds)
            results = pipe.execute()
    # Every key contributes 4 results; the count is the third.
    return [int(results[i * 4 + 2]) for i in range(len(keys))]


def check_credential_stuffing(
    auth: AuthEvent,
    redis_client,
    redis_config: Optional[RedisConfig] = None,
) -> Tuple[Optional[DetectionEvent], Optional[DetectionEvent]]:
    """
    On failed login only: update per-user and per-IP sorted sets (score = unix timestamp).
    User key: auth:fail:user:{user_id}:1m — fires when count exceeds the user threshold.
    IP key:   auth:fail:ip:{ip}:1m       — fires when count exceeds the IP threshold.
    Returns (user_scope_detection, ip_scope_detection); either or both may be set.
    """
    if auth.success:
        return None, None

    config = redis_config or RedisConfig.from_env()
    now_ts = auth.timestamp.timestamp()

    user_key = f"{USER_KEY_PREFIX}{auth.user_id}{KEY_SUFFIX}"
    ip_key = f"{IP_KEY_PREFIX}{sanitize_ip_for_key(auth.ip_address)}{KEY_SUFFIX}"
    count_user, count_ip = _window_counts(
        redis_client,
        [user_key, ip_key],
        auth.event_id,
        now_ts,
        config.auth_fail_window_seconds,
        config.auth_fail_key_ttl_seconds,
    )

    def _detection(scope: str, count: int, threshold: int) -> DetectionEvent:
        return DetectionEvent(
            detection_id=new_detection_id(f"cred-{scope}"),
            detection_type="credential_stuffing",
            severity="high",
            user_id=auth.user_id,
            transaction_id=auth.event_id,
            timestamp=datetime.now(timezone.utc),
            ip_address=auth.ip_address,
            details={
                "scope": scope,
                "count_in_window": count,
                "window_seconds": config.auth_fail_window_seconds,
                "threshold": threshold,
            },
        )

    det_user = (
        _detection("user", count_user, config.auth_fail_user_threshold)
        if count_user > config.auth_fail_user_threshold
        else None
    )
    det_ip = (
        _detection("ip", count_ip, config.auth_fail_ip_threshold)
        if count_ip > config.auth_fail_ip_threshold
        else None
    )
    return det_user, det_ip
