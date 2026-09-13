from detection_engine.geo_velocity_detector import haversine_km
from tests.factories import LA, LONDON, SF, later, tx


def _lat(redis_client, user="user_1"):
    return float(redis_client.hget(f"session:{user}", "lat"))


def test_haversine_sanity():
    assert 8500 < haversine_km(*SF, *LONDON) < 8700
    assert haversine_km(*SF, *SF) == 0


def test_lua_haversine_matches_python(make_pipeline):
    p, out, _ = make_pipeline()
    p.handle_transaction(tx(coords=SF), "r1")
    (det,) = p.handle_transaction(tx(coords=LONDON, at=later(3600)), "r2")
    assert abs(det.details["distance_km"] - haversine_km(*SF, *LONDON)) < 0.01


def test_first_transaction_sets_baseline(make_pipeline, redis_client):
    p, out, _ = make_pipeline()
    assert p.handle_transaction(tx(coords=SF), "r1") == []
    assert _lat(redis_client) == SF[0]
    assert 0 < redis_client.ttl("session:user_1") <= 7 * 86400


def test_plausible_travel_is_clean_and_advances_baseline(make_pipeline, redis_client):
    p, out, _ = make_pipeline()
    p.handle_transaction(tx(coords=SF), "r1")
    assert p.handle_transaction(tx(coords=LA, at=later(6 * 3600)), "r2") == []
    assert _lat(redis_client) == LA[0]


def test_impossible_travel_is_flagged(make_pipeline):
    p, out, _ = make_pipeline()
    p.handle_transaction(tx(coords=SF), "r1")
    (det,) = p.handle_transaction(tx(coords=LONDON, at=later(3600)), "r2")
    assert det.detection_type == "geo_velocity_anomaly"
    assert det.details["velocity_kmh"] > 900
    assert det.details["from_location"] == list(SF)


def test_anomaly_does_not_move_the_trusted_baseline(make_pipeline, redis_client):
    p, out, _ = make_pipeline(detection_cooldown_seconds=0)
    p.handle_transaction(tx(coords=SF), "r1")
    assert p.handle_transaction(tx(coords=LONDON, at=later(3600)), "r2")
    assert p.handle_transaction(tx(coords=SF, at=later(4200)), "r3") == []
    assert _lat(redis_client) == SF[0]


def test_genuine_relocation_is_accepted_once_plausible(make_pipeline, redis_client):
    p, out, _ = make_pipeline(detection_cooldown_seconds=0)
    p.handle_transaction(tx(coords=SF), "r1")
    assert p.handle_transaction(tx(coords=LONDON, at=later(3600)), "r2")
    assert p.handle_transaction(tx(coords=LONDON, at=later(12 * 3600)), "r3") == []
    assert _lat(redis_client) == LONDON[0]


def test_out_of_order_event_is_ignored_and_does_not_rewind_baseline(make_pipeline, redis_client):
    p, out, _ = make_pipeline()
    p.handle_transaction(tx(coords=LA, at=later(3600)), "r1")
    assert p.handle_transaction(tx(coords=SF, at=later(0)), "r2") == []
    assert _lat(redis_client) == LA[0]


def test_sub_second_gap_is_scored_not_skipped(make_pipeline):
    p, out, _ = make_pipeline()
    p.handle_transaction(tx(coords=SF), "r1")
    assert p.handle_transaction(tx(coords=LONDON, at=later(0.2)), "r2")


def test_threshold_is_configurable(make_pipeline):
    p, out, _ = make_pipeline(geo_max_velocity_kmh=20_000.0)
    p.handle_transaction(tx(coords=SF), "r1")
    assert p.handle_transaction(tx(coords=LONDON, at=later(3600)), "r2") == []
