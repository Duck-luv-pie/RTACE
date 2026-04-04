"""Transaction and auth event simulator: publishes to tx-events and auth-events topics."""

import logging
import random
import time
import uuid
from datetime import datetime, timezone

from common.kafka_client import create_producer, send_message
from common.models import AuthEvent, TransactionEvent
from configs.kafka_config import KafkaConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

MERCHANTS = [
    "Amazon", "Stripe", "PayPal", "Walmart", "Target",
    "Best Buy", "Netflix", "Spotify", "Uber", "Lyft",
]
# Location code -> (latitude, longitude) for geo velocity detection
LOCATION_COORDS = {
    "US-CA": (37.0, -122.0),
    "US-NY": (40.7, -74.0),
    "US-TX": (29.8, -95.4),
    "UK-LON": (51.5, -0.1),
    "DE-BER": (52.5, 13.4),
    "FR-PAR": (48.9, 2.4),
    "JP-TYO": (35.7, 139.7),
    "AU-SYD": (-33.9, 151.2),
    "US-FL": (25.8, -80.2),
    "US-WA": (47.6, -122.3),
}
LOCATIONS = list(LOCATION_COORDS.keys())

# Sample client IPs for auth events (IPv4; IPv6 would use colons — keys sanitize in detectors)
SAMPLE_IPS = [
    "198.51.100.10",
    "198.51.100.11",
    "203.0.113.50",
    "192.0.2.100",
    "192.0.2.200",
]


def generate_transaction(user_id: str) -> TransactionEvent:
    """Generate a single random transaction event."""
    location = random.choice(LOCATIONS)
    lat, lon = LOCATION_COORDS[location]
    return TransactionEvent(
        event_id=str(uuid.uuid4()),
        user_id=user_id,
        amount=round(random.uniform(5.0, 500.0), 2),
        merchant=random.choice(MERCHANTS),
        timestamp=datetime.now(timezone.utc),
        location=location,
        latitude=lat,
        longitude=lon,
    )


def generate_auth_event(user_id: str, ip_address: str, success: bool) -> AuthEvent:
    """Generate a single authentication event."""
    return AuthEvent(
        event_id=str(uuid.uuid4()),
        user_id=user_id,
        ip_address=ip_address,
        success=success,
        timestamp=datetime.now(timezone.utc),
    )


def run_simulator(
    interval_seconds: float = 2.0,
    num_users: int = 5,
    replay_probability: float = 0.2,
    auth_fail_probability: float = 0.65,
) -> None:
    """
    Run the simulator: transaction events to tx-events; auth events to auth-events.
    With replay_probability > 0, occasionally re-sends the same transaction (replay).
    auth_fail_probability controls failed vs successful logins for random auth events.
    Periodically emits bursts of failed logins to exercise credential stuffing detectors.
    """
    config = KafkaConfig.from_env()
    producer = create_producer(config)
    logger.info(
        "Starting simulator → %s + %s (interval=%.1fs, users=%d, replay_p=%.2f)",
        config.tx_events_topic,
        config.auth_events_topic,
        interval_seconds,
        num_users,
        replay_probability,
    )

    user_ids = [f"user_{i}" for i in range(1, num_users + 1)]
    last_tx_per_user: dict[str, TransactionEvent] = {}
    iteration = 0

    try:
        while True:
            iteration += 1
            user_id = random.choice(user_ids)
            # Transaction event (existing pipeline)
            if last_tx_per_user.get(user_id) and random.random() < replay_probability:
                tx = last_tx_per_user[user_id]
                logger.info("Replaying transaction for %s: event_id=%s", user_id, tx.event_id)
            else:
                tx = generate_transaction(user_id)
                last_tx_per_user[user_id] = tx

            send_message(
                producer,
                config.tx_events_topic,
                tx.model_dump(mode="json"),
                key=user_id,
            )
            logger.debug("Sent tx event_id=%s user_id=%s", tx.event_id, tx.user_id)

            # Random auth event (mostly failures to populate detectors)
            au_user = random.choice(user_ids)
            au_ip = random.choice(SAMPLE_IPS)
            success = random.random() >= auth_fail_probability
            auth = generate_auth_event(au_user, au_ip, success)
            send_message(
                producer,
                config.auth_events_topic,
                auth.model_dump(mode="json"),
                key=au_user,
            )
            logger.debug(
                "Sent auth event_id=%s user_id=%s success=%s",
                auth.event_id,
                auth.user_id,
                auth.success,
            )

            # Burst: account-targeted (11+ fails in 60s triggers user-scope; threshold default 10)
            if iteration % 45 == 0:
                burst_ip = "203.0.113.99"
                for _ in range(12):
                    fail_auth = generate_auth_event("user_1", burst_ip, success=False)
                    send_message(
                        producer,
                        config.auth_events_topic,
                        fail_auth.model_dump(mode="json"),
                        key="user_1",
                    )

            # Burst: IP-targeted (51+ fails from same IP; threshold default 50)
            if iteration % 90 == 0:
                attack_ip = "198.51.100.250"
                for u in range(52):
                    uid = f"user_{(u % num_users) + 1}"
                    fail_auth = generate_auth_event(uid, attack_ip, success=False)
                    send_message(
                        producer,
                        config.auth_events_topic,
                        fail_auth.model_dump(mode="json"),
                        key=uid,
                    )

            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        logger.info("Simulator stopped.")
    finally:
        producer.flush()
        producer.close()


if __name__ == "__main__":
    run_simulator(interval_seconds=2.0, num_users=5, replay_probability=0.2)
