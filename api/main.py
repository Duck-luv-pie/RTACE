"""FastAPI control API: health, enforcement rules, and manual overrides.

Authentication
- Set ``RTACE_API_TOKEN``. Every ``/enforcement`` endpoint then requires
  ``Authorization: Bearer <token>``.
- If it is unset (local development) read endpoints stay open and every
  mutating endpoint answers 503, so an unauthenticated deployment can never
  lift a quarantine by accident.

Every manual override is written to the audit-log topic with ``source: api``.
"""

import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import generate_latest

from common.enforcement import ip_block_key, list_rules, quarantine_key, step_up_key
from common.metrics import observe_redis_latency
from common.redis_client import create_redis_client
from configs.kafka_config import KafkaConfig
from configs.redis_config import RedisConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


# --------------------------------------------------------------------- setup


@asynccontextmanager
async def lifespan(app: FastAPI):
    """One Redis connection pool for the process, instead of one per request."""
    app.state.redis_config = RedisConfig.from_env()
    app.state.kafka_config = KafkaConfig.from_env()
    app.state.redis = create_redis_client(app.state.redis_config)
    app.state.token = os.getenv("RTACE_API_TOKEN") or None
    app.state.producer = None  # created lazily on the first manual override
    if app.state.token is None:
        logger.warning(
            "RTACE_API_TOKEN is not set: /enforcement reads are open and writes are disabled"
        )
    yield
    if app.state.producer is not None:
        try:
            app.state.producer.flush(timeout=5)
            app.state.producer.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("Producer shutdown failed: %s", e)


app = FastAPI(
    title="RTACE Control API",
    description="Real-Time Transaction Anomaly & Containment Engine",
    version="0.2.0",
    lifespan=lifespan,
)


def get_redis(request: Request):
    return request.app.state.redis


def require_read(
    request: Request, creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)
) -> None:
    token = request.app.state.token
    if token is None:
        return
    if creds is None or not secrets.compare_digest(creds.credentials, token):
        raise HTTPException(status_code=401, detail="Missing or invalid bearer token")


def require_write(
    request: Request, creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)
) -> None:
    if request.app.state.token is None:
        raise HTTPException(
            status_code=503,
            detail="Manual overrides are disabled: set RTACE_API_TOKEN to enable them",
        )
    require_read(request, creds)


def _audit(request: Request, action: str, **fields: Any) -> None:
    """Best-effort audit record for a manual override."""
    record = {
        "source": "api",
        "action": action,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "client": request.client.host if request.client else None,
        **fields,
    }
    logger.info("Manual override: %s", record)
    state = request.app.state
    try:
        if state.producer is None:
            from common.kafka_client import create_producer  # local: keep import cost off /health

            state.producer = create_producer(state.kafka_config)
        from common.kafka_client import send_message

        send_message(
            state.producer,
            state.kafka_config.audit_log_topic,
            record,
            key=fields.get("user_id") or fields.get("ip"),
        )
    except Exception as e:  # noqa: BLE001 - the override already happened; do not fail it
        logger.error("Could not write audit record for manual override: %s", e)


# ------------------------------------------------------------------- routes


@app.get("/metrics")
def metrics() -> Response:
    """Prometheus scrape endpoint."""
    return Response(content=generate_latest(), media_type="text/plain; charset=utf-8")


@app.get("/health")
def health(r=Depends(get_redis)) -> dict[str, str]:
    """Liveness/readiness: checks Redis connectivity."""
    try:
        with observe_redis_latency("ping"):
            r.ping()
        return {"status": "ok", "redis": "connected"}
    except Exception as e:
        logger.exception("Health check failed: %s", e)
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/enforcement/rules", dependencies=[Depends(require_read)])
def get_rules(r=Depends(get_redis)) -> dict[str, Any]:
    """All active quarantines, pending step-ups and IP blocks, with TTLs."""
    return list_rules(r)


@app.post("/enforcement/quarantine/{user_id}", dependencies=[Depends(require_write)], status_code=201)
def add_quarantine(
    user_id: str,
    request: Request,
    ttl_seconds: Optional[int] = Query(default=None, ge=1, le=7 * 24 * 3600),
    reason: str = Query(default="manual"),
    r=Depends(get_redis),
) -> dict[str, Any]:
    ttl = ttl_seconds or request.app.state.redis_config.quarantine_ttl_seconds
    r.setex(quarantine_key(user_id), ttl, f"api:{reason}")
    _audit(request, "manual_quarantine", user_id=user_id, ttl_seconds=ttl, reason=reason)
    return {"user_id": user_id, "action": "quarantine", "ttl_seconds": ttl}


@app.delete("/enforcement/quarantine/{user_id}", dependencies=[Depends(require_write)])
def lift_quarantine(user_id: str, request: Request, r=Depends(get_redis)) -> dict[str, Any]:
    removed = r.delete(quarantine_key(user_id))
    if not removed:
        raise HTTPException(status_code=404, detail=f"No quarantine for user {user_id}")
    _audit(request, "lift_quarantine", user_id=user_id)
    return {"user_id": user_id, "action": "lift_quarantine"}


@app.delete("/enforcement/step-up/{user_id}", dependencies=[Depends(require_write)])
def clear_step_up(user_id: str, request: Request, r=Depends(get_redis)) -> dict[str, Any]:
    """Call once the user has completed step-up authentication."""
    removed = r.delete(step_up_key(user_id))
    if not removed:
        raise HTTPException(status_code=404, detail=f"No pending step-up for user {user_id}")
    _audit(request, "clear_step_up", user_id=user_id)
    return {"user_id": user_id, "action": "clear_step_up"}


@app.post("/enforcement/ip-block/{ip}", dependencies=[Depends(require_write)], status_code=201)
def add_ip_block(
    ip: str,
    request: Request,
    ttl_seconds: Optional[int] = Query(default=None, ge=1, le=30 * 24 * 3600),
    reason: str = Query(default="manual"),
    r=Depends(get_redis),
) -> dict[str, Any]:
    ttl = ttl_seconds or request.app.state.redis_config.ip_block_ttl_seconds
    r.setex(ip_block_key(ip), ttl, f"api:{reason}")
    _audit(request, "manual_ip_block", ip=ip, ttl_seconds=ttl, reason=reason)
    return {"ip": ip, "action": "ip_block", "ttl_seconds": ttl}


@app.delete("/enforcement/ip-block/{ip}", dependencies=[Depends(require_write)])
def lift_ip_block(ip: str, request: Request, r=Depends(get_redis)) -> dict[str, Any]:
    removed = r.delete(ip_block_key(ip))
    if not removed:
        raise HTTPException(status_code=404, detail=f"No block for IP {ip}")
    _audit(request, "lift_ip_block", ip=ip)
    return {"ip": ip, "action": "lift_ip_block"}


@app.get("/")
def root() -> dict[str, str]:
    """API info."""
    return {
        "service": "RTACE Control API",
        "docs": "/docs",
        "health": "/health",
        "metrics": "/metrics",
        "enforcement": "/enforcement/rules",
    }
