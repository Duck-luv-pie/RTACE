"""Transaction simulator: generates events and publishes to Kafka tx-events topic."""

import logging
import random
import time
import uuid
from datetime import datetime, timezone

from common.kafka_client import create_producer, send_message
from common.models import TransactionEvent
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


def run_simulator(
    interval_seconds: float = 2.0,
    num_users: int = 5,
    replay_probability: float = 0.2,
) -> None:
    """
    Run the transaction simulator.
    With replay_probability > 0, occasionally re-sends the same transaction (simulating replay).
    """
    config = KafkaConfig.from_env()
    producer = create_producer(config)
    logger.info(
        "Starting simulator → %s (interval=%.1fs, users=%d, replay_p=%.2f)",
        config.tx_events_topic,
        interval_seconds,
        num_users,
        replay_probability,
    )

    user_ids = [f"user_{i}" for i in range(1, num_users + 1)]
    last_tx_per_user: dict[str, TransactionEvent] = {}

    try:
        while True:
            user_id = random.choice(user_ids)
            # Optionally replay the last transaction for this user
            if last_tx_per_user.get(user_id) and random.random() < replay_probability:
                tx = last_tx_per_user[user_id]
                logger.info("Replaying transaction for %s: event_id=%s", user_id, tx.event_id)
            else:
                tx = generate_transaction(user_id)
                last_tx_per_user[user_id] = tx

            payload = tx.model_dump(mode="json")
            send_message(producer, config.tx_events_topic, payload, key=user_id)
            logger.debug("Sent tx event_id=%s user_id=%s amount=%s", tx.event_id, tx.user_id, tx.amount)
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        logger.info("Simulator stopped.")
    finally:
        producer.flush()
        producer.close()


if __name__ == "__main__":
    run_simulator(interval_seconds=2.0, num_users=5, replay_probability=0.2)
