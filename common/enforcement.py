"""Enforcement state in Redis: key layout, reads used by the detection engine and the API.

Writes live in containment_engine.containment_actions (automatic) and api.main
(manual). Everything here is read-only so the detection engine never depends
on the containment package.
"""

from typing import Any

from common.metrics import observe_redis_latency

QUARANTINE_KEY_PREFIX = "enforce:quarantine:user:"
STEP_UP_KEY_PREFIX = "enforce:step_up:user:"
IP_BLOCK_KEY_PREFIX = "block:ip:"


def sanitize_ip_for_key(ip: str) -> str:
    """IPv6 addresses contain colons, which are the Redis key separator by convention."""
    return ip.replace(":", "-")


def quarantine_key(user_id: str) -> str:
    return f"{QUARANTINE_KEY_PREFIX}{user_id}"


def step_up_key(user_id: str) -> str:
    return f"{STEP_UP_KEY_PREFIX}{user_id}"


def ip_block_key(ip: str) -> str:
    return f"{IP_BLOCK_KEY_PREFIX}{sanitize_ip_for_key(ip)}"


def is_user_quarantined(redis_client, user_id: str) -> bool:
    with observe_redis_latency("enforce_check_user"):
        return bool(redis_client.exists(quarantine_key(user_id)))


def is_ip_blocked(redis_client, ip: str) -> bool:
    with observe_redis_latency("enforce_check_ip"):
        return bool(redis_client.exists(ip_block_key(ip)))


def is_step_up_pending(redis_client, user_id: str) -> bool:
    with observe_redis_latency("enforce_check_step_up"):
        return bool(redis_client.exists(step_up_key(user_id)))


def list_rules(redis_client) -> dict[str, Any]:
    """Every active rule with its remaining TTL and the detection that set it.

    SCAN (never KEYS) to find the keys, then one pipelined round trip for all
    TTL/GET calls instead of two calls per key.
    """
    groups = {
        "quarantines": (QUARANTINE_KEY_PREFIX, "user_id"),
        "step_ups": (STEP_UP_KEY_PREFIX, "user_id"),
        "ip_blocks": (IP_BLOCK_KEY_PREFIX, "ip"),
    }
    result: dict[str, Any] = {}
    with observe_redis_latency("scan_enforcement"):
        for name, (prefix, subject_field) in groups.items():
            keys = list(redis_client.scan_iter(match=f"{prefix}*", count=500))
            with redis_client.pipeline(transaction=False) as pipe:
                for key in keys:
                    pipe.ttl(key)
                    pipe.get(key)
                flat = pipe.execute()
            rules = []
            for i, key in enumerate(keys):
                subject = key[len(prefix):]
                if subject_field == "ip" and "-" in subject:
                    subject = subject.replace("-", ":")  # IPv4 never has dashes: this is IPv6
                rules.append(
                    {
                        subject_field: subject,
                        "key": key,
                        "ttl_seconds": flat[2 * i],
                        "set_by": flat[2 * i + 1],
                    }
                )
            result[name] = rules
    result["count"] = sum(len(v) for v in result.values())
    return result
