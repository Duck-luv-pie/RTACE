"""Shared Pydantic models for RTACE."""

import hashlib
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class TransactionEvent(BaseModel):
    """Incoming transaction event from the simulator."""

    event_id: str
    user_id: str
    amount: float
    merchant: str
    timestamp: datetime
    location: str

    def replay_hash(self) -> str:
        """Stable hash for replay detection (same payload = same hash)."""
        payload = f"{self.user_id}|{self.amount}|{self.merchant}|{self.timestamp.isoformat()}|{self.location}"
        return hashlib.sha256(payload.encode()).hexdigest()


class DetectionEvent(BaseModel):
    """Detection event published to the detections topic."""

    detection_id: str
    detection_type: str = Field(..., description="e.g. replay_attack")
    severity: str = Field(..., description="e.g. high, medium, low")
    user_id: str
    transaction_id: str
    timestamp: datetime
