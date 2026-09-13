from dataclasses import replace

from detection_engine.replay_detector import REDIS_KEY_PREFIX, check_replay
from tests.factories import tx


def test_first_sighting_is_clean(redis_client, redis_config):
    assert check_replay(tx(), redis_client, redis_config, record_ref="tx:0:1") is None


def test_same_payload_in_a_different_record_is_a_replay(redis_client, redis_config):
    original = tx()
    check_replay(original, redis_client, redis_config, record_ref="tx:0:1")
    det = check_replay(original, redis_client, redis_config, record_ref="tx:0:9")
    assert det is not None
    assert det.detection_type == "replay_attack"
    assert det.transaction_id == original.event_id
    assert det.details["first_seen_record"] == "tx:0:1"


def test_redelivery_of_the_same_record_is_not_a_replay(redis_client, redis_config):
    original = tx()
    check_replay(original, redis_client, redis_config, record_ref="tx:0:1")
    # Kafka redelivers the very same record after a crash
    assert check_replay(original, redis_client, redis_config, record_ref="tx:0:1") is None


def test_replay_after_redelivery_is_still_caught(redis_client, redis_config):
    original = tx()
    check_replay(original, redis_client, redis_config, record_ref="tx:0:1")
    check_replay(original, redis_client, redis_config, record_ref="tx:0:1")
    assert check_replay(original, redis_client, redis_config, record_ref="tx:0:2") is not None


def test_without_record_ref_every_repeat_is_a_replay(redis_client, redis_config):
    original = tx()
    assert check_replay(original, redis_client, redis_config) is None
    assert check_replay(original, redis_client, redis_config) is not None


def test_detection_ids_are_unique_per_detection(redis_client, redis_config):
    original = tx()
    check_replay(original, redis_client, redis_config, record_ref="a")
    d1 = check_replay(original, redis_client, redis_config, record_ref="b")
    d2 = check_replay(original, redis_client, redis_config, record_ref="c")
    assert d1.detection_id != d2.detection_id


def test_key_has_sliding_ttl_and_is_not_refreshed_by_repeats(redis_client, redis_config):
    cfg = replace(redis_config, replay_ttl_hours=2)
    original = tx()
    check_replay(original, redis_client, cfg, record_ref="a")
    key = f"{REDIS_KEY_PREFIX}{original.replay_hash()}"
    ttl_before = redis_client.ttl(key)
    assert 0 < ttl_before <= 2 * 3600
    check_replay(original, redis_client, cfg, record_ref="b")
    assert redis_client.ttl(key) <= ttl_before


def test_different_transactions_do_not_collide(redis_client, redis_config):
    check_replay(tx(amount=10.0), redis_client, redis_config, record_ref="a")
    assert check_replay(tx(amount=10.01), redis_client, redis_config, record_ref="b") is None
