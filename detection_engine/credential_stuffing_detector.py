"""Credential stuffing detection: failed login bursts per user or per IP (rolling window)."""

from datetime import datetime, timezone
from typing import Optional, Tuple

from common.metrics import observe_redis_latency
from common.models import AuthEvent, DetectionEvent
from configs.redis_config import RedisConfig

USER_KEY_PREFIX = "auth:fail:user:"
IP_KEY_PREFIX = "auth:fail:ip:"
KEY_SUFFIX = ":1m"


def _sanitize_ip_for_key(ip: str) -> str:
    """Avoid ambiguous colons in Redis keys for IPv6."""
    return ip.replace(":", "-")


def _track_fail_sorted_set(
    redis_client,
    key: str,
    member: str,
    now_ts: float,
    window_seconds: int,
    key_ttl_seconds: int,
) -> int:
    """Trim old members, add current attempt, expire key; return count after add."""
    cutoff = now_ts - float(window_seconds)
    with observe_redis_latency("credential_stuffing_window"):
        redis_client.zremrangebyscore(key, "-inf", cutoff)
        redis_client.zadd(key, {member: now_ts})
        count = redis_client.zcard(key)
        redis_client.expire(key, key_ttl_seconds)
    return count


def check_credential_stuffing(
    auth: AuthEvent,
    redis_client,
    redis_config: Optional[RedisConfig] = None,
) -> Tuple[Optional[DetectionEvent], Optional[DetectionEvent]]:
    """
    On failed login only: update per-user and per-IP sorted sets (score = unix timestamp).
    User key: auth:fail:user:{user_id}:1m — threshold default 11+ fails in 60s.
    IP key: auth:fail:ip:{ip}:1m — threshold default 51+ fails in 60s.
    Returns (user_scope_detection, ip_scope_detection); either or both may be set.
    """
    if auth.success:
        return None, None

    config = redis_config or RedisConfig.from_env()
    now_ts = auth.timestamp.timestamp()
    ttl = config.auth_fail_key_ttl_seconds
    window = config.auth_fail_window_seconds

    user_key = f"{USER_KEY_PREFIX}{auth.user_id}{KEY_SUFFIX}"
    count_user = _track_fail_sorted_set(
        redis_client, user_key, auth.event_id, now_ts, window, ttl
    )
    det_user: Optional[DetectionEvent] = None
    if count_user > config.auth_fail_user_threshold:
        det_user = DetectionEvent(
            detection_id=f"det-cred-user-{auth.event_id}",
            detection_type="credential_stuffing",
            severity="high",
            user_id=auth.user_id,
            transaction_id=auth.event_id,
            timestamp=datetime.now(timezone.utc),
            ip_address=auth.ip_address,
        )

    ip_part = _sanitize_ip_for_key(auth.ip_address)
    ip_key = f"{IP_KEY_PREFIX}{ip_part}{KEY_SUFFIX}"
    count_ip = _track_fail_sorted_set(
        redis_client, ip_key, auth.event_id, now_ts, window, ttl
    )
    det_ip: Optional[DetectionEvent] = None
    if count_ip > config.auth_fail_ip_threshold:
        det_ip = DetectionEvent(
            detection_id=f"det-cred-ip-{auth.event_id}",
            detection_type="credential_stuffing",
            severity="high",
            user_id=auth.user_id,
            transaction_id=auth.event_id,
            timestamp=datetime.now(timezone.utc),
            ip_address=auth.ip_address,
        )

    return det_user, det_ip
