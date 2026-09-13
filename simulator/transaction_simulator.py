"""Transaction and auth event simulator: publishes to tx-events and auth-events.

Traffic model
- Every user has a home city. Almost all of their transactions come from home,
  so the geo-velocity detector sees realistic traffic instead of a random world
  city per event (which used to make impossible travel fire on nearly every
  transaction and drown out everything else).
- Attacks are injected explicitly and logged with a ``scenario=`` tag so you
  can line up simulator output with detections:

    replay              re-send a user's previous transaction verbatim
    impossible_travel   one transaction from a city thousands of km from home
    fraud_burst         BURST_SIZE transactions for one user in a few seconds
                        (> REDIS_BURST_THRESHOLD, which the old simulator's
                        5 users at one tx per 2 s could never reach)
    stuffing_user       12 failed logins against one account from one IP
    stuffing_ip         52 failed logins from one IP across many accounts

Configuration: environment variables (SIM_*) or CLI flags; flags win.
Quarantined users keep sending, which is realistic (an attacker keeps trying)
and exercises enforcement: those transactions show up as blocked.
"""

import argparse
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Optional, Union

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
# Location code -> (latitude, longitude)
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

SAMPLE_IPS = ["198.51.100.10", "198.51.100.11", "203.0.113.50", "192.0.2.100", "192.0.2.200"]
STUFFING_USER_IP = "203.0.113.99"
STUFFING_ATTACK_IP = "198.51.100.250"

BURST_SIZE = 25
STUFFING_USER_FAILS = 12
STUFFING_IP_FAILS = 52


@dataclass
class SimulatorSettings:
    interval_seconds: float = 0.5
    num_users: int = 20
    replay_probability: float = 0.05
    impossible_travel_probability: float = 0.02
    auth_fail_probability: float = 0.10
    burst_every: int = 200          # iterations between fraud_burst scenarios (0 = never)
    stuffing_user_every: int = 150  # iterations between account-targeted stuffing (0 = never)
    stuffing_ip_every: int = 400    # iterations between IP-targeted stuffing (0 = never)
    seed: Optional[int] = None

    @classmethod
    def from_env(cls) -> "SimulatorSettings":
        d = cls()
        return cls(
            interval_seconds=float(os.getenv("SIM_INTERVAL_SECONDS", d.interval_seconds)),
            num_users=int(os.getenv("SIM_USERS", d.num_users)),
            replay_probability=float(os.getenv("SIM_REPLAY_PROBABILITY", d.replay_probability)),
            impossible_travel_probability=float(
                os.getenv("SIM_IMPOSSIBLE_TRAVEL_PROBABILITY", d.impossible_travel_probability)
            ),
            auth_fail_probability=float(os.getenv("SIM_AUTH_FAIL_PROBABILITY", d.auth_fail_probability)),
            burst_every=int(os.getenv("SIM_BURST_EVERY", d.burst_every)),
            stuffing_user_every=int(os.getenv("SIM_STUFFING_USER_EVERY", d.stuffing_user_every)),
            stuffing_ip_every=int(os.getenv("SIM_STUFFING_IP_EVERY", d.stuffing_ip_every)),
            seed=int(os.environ["SIM_SEED"]) if os.getenv("SIM_SEED") else None,
        )


Event = Union[TransactionEvent, AuthEvent]


@dataclass
class Emitted:
    event: Event
    scenario: str  # "normal" or one of the injected scenario names


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_transaction(rng: random.Random, user_id: str, location: str) -> TransactionEvent:
    lat, lon = LOCATION_COORDS[location]
    return TransactionEvent(
        event_id=str(uuid.uuid4()),
        user_id=user_id,
        amount=round(rng.uniform(5.0, 500.0), 2),
        merchant=rng.choice(MERCHANTS),
        timestamp=_now(),
        location=location,
        latitude=lat,
        longitude=lon,
    )


def make_auth(user_id: str, ip_address: str, success: bool) -> AuthEvent:
    return AuthEvent(
        event_id=str(uuid.uuid4()),
        user_id=user_id,
        ip_address=ip_address,
        success=success,
        timestamp=_now(),
    )


def generate_events(settings: SimulatorSettings, rng: Optional[random.Random] = None) -> Iterator[Emitted]:
    """Infinite stream of events. One "iteration" is one normal transaction plus
    one auth event, with scenarios interleaved on their schedules."""
    rng = rng or random.Random(settings.seed)
    user_ids = [f"user_{i}" for i in range(1, settings.num_users + 1)]
    home = {u: rng.choice(LOCATIONS) for u in user_ids}
    last_tx: dict[str, TransactionEvent] = {}
    iteration = 0

    while True:
        iteration += 1
        user_id = rng.choice(user_ids)

        # --- one transaction: replay, impossible travel, or normal
        roll = rng.random()
        if roll < settings.replay_probability and user_id in last_tx:
            yield Emitted(last_tx[user_id], "replay")
        elif settings.replay_probability <= roll < (
            settings.replay_probability + settings.impossible_travel_probability
        ):
            away = rng.choice([loc for loc in LOCATIONS if loc != home[user_id]])
            tx = make_transaction(rng, user_id, away)
            # deliberately not stored as last_tx: the next normal tx is from home again
            yield Emitted(tx, "impossible_travel")
        else:
            tx = make_transaction(rng, user_id, home[user_id])
            last_tx[user_id] = tx
            yield Emitted(tx, "normal")

        # --- one auth event, mostly successful
        au_user = rng.choice(user_ids)
        success = rng.random() >= settings.auth_fail_probability
        yield Emitted(make_auth(au_user, rng.choice(SAMPLE_IPS), success), "normal")

        # --- scheduled scenarios
        if settings.burst_every and iteration % settings.burst_every == 0:
            victim = rng.choice(user_ids)
            for _ in range(BURST_SIZE):
                yield Emitted(make_transaction(rng, victim, home[victim]), "fraud_burst")

        if settings.stuffing_user_every and iteration % settings.stuffing_user_every == 0:
            victim = rng.choice(user_ids)
            for _ in range(STUFFING_USER_FAILS):
                yield Emitted(make_auth(victim, STUFFING_USER_IP, success=False), "stuffing_user")

        if settings.stuffing_ip_every and iteration % settings.stuffing_ip_every == 0:
            for i in range(STUFFING_IP_FAILS):
                uid = user_ids[i % len(user_ids)]
                yield Emitted(make_auth(uid, STUFFING_ATTACK_IP, success=False), "stuffing_ip")

        yield Emitted(None, "__sleep__")  # type: ignore[arg-type]  # pacing marker


def run_simulator(settings: Optional[SimulatorSettings] = None) -> None:
    settings = settings or SimulatorSettings.from_env()
    config = KafkaConfig.from_env()
    producer = create_producer(config)
    logger.info("Starting simulator → %s + %s with %s", config.tx_events_topic, config.auth_events_topic, settings)

    sent = 0
    try:
        for item in generate_events(settings):
            if item.scenario == "__sleep__":
                time.sleep(settings.interval_seconds)
                continue
            ev = item.event
            if isinstance(ev, TransactionEvent):
                send_message(producer, config.tx_events_topic, ev.model_dump(mode="json"), key=ev.user_id)
            else:
                send_message(producer, config.auth_events_topic, ev.model_dump(mode="json"), key=ev.user_id)
            sent += 1
            if item.scenario != "normal":
                logger.info(
                    "scenario=%s user_id=%s event_id=%s%s",
                    item.scenario,
                    ev.user_id,
                    ev.event_id,
                    f" location={ev.location}" if isinstance(ev, TransactionEvent) else f" ip={ev.ip_address}",
                )
            elif sent % 500 == 0:
                logger.info("Sent %d events", sent)
    except KeyboardInterrupt:
        logger.info("Simulator stopped after %d events.", sent)
    finally:
        producer.flush()
        producer.close()


def _parse_args(argv=None) -> SimulatorSettings:
    env = SimulatorSettings.from_env()
    p = argparse.ArgumentParser(description="RTACE traffic simulator (env SIM_* variables set the defaults)")
    p.add_argument("--interval", type=float, default=env.interval_seconds, help="seconds between iterations")
    p.add_argument("--users", type=int, default=env.num_users)
    p.add_argument("--replay-p", type=float, default=env.replay_probability)
    p.add_argument("--travel-p", type=float, default=env.impossible_travel_probability)
    p.add_argument("--auth-fail-p", type=float, default=env.auth_fail_probability)
    p.add_argument("--burst-every", type=int, default=env.burst_every, help="0 disables")
    p.add_argument("--stuffing-user-every", type=int, default=env.stuffing_user_every, help="0 disables")
    p.add_argument("--stuffing-ip-every", type=int, default=env.stuffing_ip_every, help="0 disables")
    p.add_argument("--seed", type=int, default=env.seed)
    a = p.parse_args(argv)
    return SimulatorSettings(
        interval_seconds=a.interval,
        num_users=a.users,
        replay_probability=a.replay_p,
        impossible_travel_probability=a.travel_p,
        auth_fail_probability=a.auth_fail_p,
        burst_every=a.burst_every,
        stuffing_user_every=a.stuffing_user_every,
        stuffing_ip_every=a.stuffing_ip_every,
        seed=a.seed,
    )


if __name__ == "__main__":
    run_simulator(_parse_args())
