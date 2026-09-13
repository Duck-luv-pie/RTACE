"""Credential stuffing detection: failed login bursts per user or per IP (rolling window).

lua/auth_check.lua maintains both sorted sets in one atomic call and returns
the two counts; this module turns them into up to two DetectionEvents.
"""

from datetime import datetime, timezone
from typing import Optional, Tuple

from common.models import AuthEvent, DetectionEvent, new_detection_id
from configs.redis_config import RedisConfig

USER_KEY_PREFIX = "auth:fail:user:"
IP_KEY_PREFIX = "auth:fail:ip:"
KEY_SUFFIX = ":1m"


def sanitize_ip_for_key(ip: str) -> str:
    """Avoid ambiguous colons in Redis keys for IPv6."""
    return ip.replace(":", "-")


def user_key(auth: AuthEvent) -> str:
    return f"{USER_KEY_PREFIX}{auth.user_id}{KEY_SUFFIX}"


def ip_key(auth: AuthEvent) -> str:
    return f"{IP_KEY_PREFIX}{sanitize_ip_for_key(auth.ip_address)}{KEY_SUFFIX}"


def interpret(
    auth: AuthEvent, count_user: int, count_ip: int, config: RedisConfig
) -> Tuple[Optional[DetectionEvent], Optional[DetectionEvent]]:
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
                "count_in_window": int(count),
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
