"""FastAPI control API: health, status, and enforcement rules."""

import logging
from typing import Any

from fastapi import FastAPI, HTTPException

from common.redis_client import create_redis_client
from containment_engine.containment_actions import QUARANTINE_KEY_PREFIX

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="RTACE Control API",
    description="Real-Time Transaction Anomaly & Containment Engine",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness/readiness: checks Redis connectivity."""
    try:
        r = create_redis_client()
        r.ping()
        return {"status": "ok", "redis": "connected"}
    except Exception as e:
        logger.exception("Health check failed: %s", e)
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/enforcement/rules")
def list_quarantine_rules() -> dict[str, Any]:
    """List current user quarantine rules from Redis (enforce:quarantine:user:*)."""
    try:
        r = create_redis_client()
        keys = list(r.scan_iter(match=f"{QUARANTINE_KEY_PREFIX}*"))
        rules = []
        for key in keys:
            ttl = r.ttl(key)
            user_id = key.replace(QUARANTINE_KEY_PREFIX, "")
            rules.append({"user_id": user_id, "key": key, "ttl_seconds": ttl})
        return {"count": len(rules), "rules": rules}
    except Exception as e:
        logger.exception("Failed to list rules: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
def root() -> dict[str, str]:
    """API info."""
    return {
        "service": "RTACE Control API",
        "docs": "/docs",
        "health": "/health",
        "enforcement": "/enforcement/rules",
    }
