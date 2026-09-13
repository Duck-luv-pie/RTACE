from dataclasses import replace

import pytest

from common import session_cache
from detection_engine.geo_velocity_detector import check_geo_velocity, haversine_km
from tests.factories import LA, LONDON, SF, later, tx


@pytest.fixture(autouse=True)
def _fresh_cache():
    session_cache.clear()
    yield
    session_cache.clear()


def test_haversine_sanity():
    assert 8500 < haversine_km(*SF, *LONDON) < 8700
    assert haversine_km(*SF, *SF) == 0


def test_first_transaction_sets_baseline(redis_client, redis_config):
    assert check_geo_velocity(tx(coords=SF), redis_client, redis_config) is None
    assert redis_client.hgetall("session:user_1")["last_latitude"] == str(SF[0])


def test_plausible_travel_is_clean_and_advances_baseline(redis_client, redis_config):
    check_geo_velocity(tx(coords=SF), redis_client, redis_config)
    # SF -> LA (~560 km) in 6 hours
    assert check_geo_velocity(tx(coords=LA, at=later(6 * 3600)), redis_client, redis_config) is None
    assert redis_client.hgetall("session:user_1")["last_latitude"] == str(LA[0])


def test_impossible_travel_is_flagged(redis_client, redis_config):
    check_geo_velocity(tx(coords=SF), redis_client, redis_config)
    det = check_geo_velocity(tx(coords=LONDON, at=later(3600)), redis_client, redis_config)
    assert det is not None
    assert det.detection_type == "geo_velocity_anomaly"
    assert det.details["velocity_kmh"] > 900


def test_anomaly_does_not_move_the_trusted_baseline(redis_client, redis_config):
    """A flagged location must not become the baseline, or the user's next
    legitimate transaction from home is flagged too (ping-pong)."""
    check_geo_velocity(tx(coords=SF), redis_client, redis_config)
    assert check_geo_velocity(tx(coords=LONDON, at=later(3600)), redis_client, redis_config)
    # Back home 10 minutes later: consistent with the SF baseline, not an anomaly
    assert check_geo_velocity(tx(coords=SF, at=later(4200)), redis_client, redis_config) is None
    assert redis_client.hgetall("session:user_1")["last_latitude"] == str(SF[0])


def test_genuine_relocation_is_accepted_once_plausible(redis_client, redis_config):
    check_geo_velocity(tx(coords=SF), redis_client, redis_config)
    assert check_geo_velocity(tx(coords=LONDON, at=later(3600)), redis_client, redis_config)
    # 12 hours later the same London location implies ~720 km/h: plausible
    assert check_geo_velocity(tx(coords=LONDON, at=later(12 * 3600)), redis_client, redis_config) is None
    assert redis_client.hgetall("session:user_1")["last_latitude"] == str(LONDON[0])


def test_out_of_order_event_is_ignored_and_does_not_rewind_baseline(redis_client, redis_config):
    check_geo_velocity(tx(coords=LA, at=later(3600)), redis_client, redis_config)
    # An older event arrives late
    assert check_geo_velocity(tx(coords=SF, at=later(0)), redis_client, redis_config) is None
    assert redis_client.hgetall("session:user_1")["last_latitude"] == str(LA[0])


def test_sub_second_gap_is_scored_not_skipped(redis_client, redis_config):
    check_geo_velocity(tx(coords=SF), redis_client, redis_config)
    assert check_geo_velocity(tx(coords=LONDON, at=later(0.2)), redis_client, redis_config) is not None


def test_threshold_is_configurable(redis_client, redis_config):
    lenient = replace(redis_config, geo_max_velocity_kmh=20_000.0)
    check_geo_velocity(tx(coords=SF), redis_client, lenient)
    assert check_geo_velocity(tx(coords=LONDON, at=later(3600)), redis_client, lenient) is None
