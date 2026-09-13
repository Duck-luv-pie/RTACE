"""Small builders for test events."""

from datetime import datetime, timedelta, timezone
import itertools

from common.models import AuthEvent, TransactionEvent

T0 = datetime(2025, 3, 8, 12, 0, 0, tzinfo=timezone.utc)
_seq = itertools.count(1)

# A few real-world coordinates
SF = (37.77, -122.42)
LA = (34.05, -118.24)       # ~560 km from SF
LONDON = (51.51, -0.13)     # ~8600 km from SF
NYC = (40.71, -74.01)


def tx(
    user_id: str = "user_1",
    at: datetime = T0,
    coords=SF,
    amount: float = 42.5,
    merchant: str = "Amazon",
    location: str = "US-CA",
    event_id: str | None = None,
) -> TransactionEvent:
    return TransactionEvent(
        event_id=event_id or f"evt-{next(_seq)}",
        user_id=user_id,
        amount=amount,
        merchant=merchant,
        timestamp=at,
        location=location,
        latitude=coords[0],
        longitude=coords[1],
    )


def auth(
    user_id: str = "user_1",
    ip: str = "198.51.100.10",
    success: bool = False,
    at: datetime = T0,
    event_id: str | None = None,
) -> AuthEvent:
    return AuthEvent(
        event_id=event_id or f"auth-{next(_seq)}",
        user_id=user_id,
        ip_address=ip,
        success=success,
        timestamp=at,
    )


def later(seconds: float, base: datetime = T0) -> datetime:
    return base + timedelta(seconds=seconds)
