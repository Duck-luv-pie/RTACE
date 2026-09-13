"""Model round-trip tests."""

from datetime import datetime, timezone

from common.models import AuthEvent, DetectionEvent, TransactionEvent


def _tx(**overrides) -> TransactionEvent:
    base = dict(
        event_id="evt-1",
        user_id="user_1",
        amount=42.5,
        merchant="Amazon",
        timestamp=datetime(2025, 3, 8, 12, 0, tzinfo=timezone.utc),
        location="US-CA",
        latitude=37.0,
        longitude=-122.0,
    )
    base.update(overrides)
    return TransactionEvent(**base)


def test_transaction_round_trips_through_json():
    tx = _tx()
    again = TransactionEvent.model_validate(tx.model_dump(mode="json"))
    assert again == tx


def test_replay_hash_is_stable_across_serialisation():
    tx = _tx()
    again = TransactionEvent.model_validate(tx.model_dump(mode="json"))
    assert again.replay_hash() == tx.replay_hash()


def test_replay_hash_changes_when_payload_changes():
    assert _tx().replay_hash() != _tx(amount=42.51).replay_hash()


def test_auth_and_detection_round_trip():
    auth = AuthEvent(
        event_id="a-1",
        user_id="user_1",
        ip_address="198.51.100.1",
        success=False,
        timestamp=datetime.now(timezone.utc),
    )
    assert AuthEvent.model_validate(auth.model_dump(mode="json")) == auth

    det = DetectionEvent(
        detection_id="det-1",
        detection_type="replay_attack",
        severity="high",
        user_id="user_1",
        transaction_id="evt-1",
        timestamp=datetime.now(timezone.utc),
    )
    assert DetectionEvent.model_validate(det.model_dump(mode="json")) == det
