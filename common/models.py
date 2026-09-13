"""Shared Pydantic models for RTACE."""

import hashlib
import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


def new_detection_id(prefix: str) -> str:
    """Globally unique detection id. Detection ids must never be derived from the
    triggering event id alone: the same event can legitimately produce several
    detections (a replayed transaction replayed again, a burst that keeps going)."""
    return f"det-{prefix}-{uuid.uuid4()}"


_DETECTION_ID_NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # NAMESPACE_URL


def stable_detection_id(prefix: str, ref: str, detection_type: str, scope: str = "") -> str:
    """Deterministic detection id for one (Kafka delivery, detector, scope).

    A record redelivered by Kafka, or a batch reprocessed after a produce
    failure, must yield the SAME detection id so that containment (keyed by
    detection id) applies it exactly once. Derived from the delivery ref plus
    the detector identity, which is unique per record and per detector output.
    """
    identity = f"{ref}:{detection_type}:{scope}"
    return f"det-{prefix}-{uuid.uuid5(_DETECTION_ID_NS, identity)}"


class TransactionEvent(BaseModel):
    """Incoming transaction event from the simulator."""

    event_id: str
    user_id: str
    amount: float
    merchant: str
    timestamp: datetime
    location: str
    latitude: float
    longitude: float

    def replay_hash(self) -> str:
        """Stable hash identifying this exact request for replay detection.

        The timestamp is deliberately part of the hash. A replay attack resends
        a captured request byte-for-byte, timestamp included; a request whose
        timestamp differs is a *new* request as far as the upstream signature is
        concerned. Dropping the timestamp would turn two legitimate identical
        purchases on the same day into a false positive.
        """
        payload = (
            f"{self.user_id}|{self.amount:.2f}|{self.merchant}|"
            f"{self.timestamp.isoformat()}|{self.location}"
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class AuthEvent(BaseModel):
    """Authentication / login event (separate from transaction events)."""

    event_id: str
    user_id: str
    ip_address: str
    success: bool
    timestamp: datetime


class DetectionEvent(BaseModel):
    """Detection event published to the detections topic."""

    detection_id: str
    detection_type: str = Field(..., description="e.g. replay_attack")
    severity: str = Field(..., description="e.g. high, medium, low")
    user_id: str
    transaction_id: str
    timestamp: datetime
    ip_address: Optional[str] = Field(
        default=None,
        description="Source IP when relevant (e.g. credential stuffing)",
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Detector-specific evidence (velocity, counts, scope, ...)",
    )
