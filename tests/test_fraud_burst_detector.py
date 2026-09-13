from dataclasses import replace

from detection_engine.fraud_burst_detector import check_fraud_burst
from tests.factories import later, tx


def test_fires_only_when_count_exceeds_threshold(redis_client, redis_config):
    cfg = replace(redis_config, burst_threshold=3)
    results = [check_fraud_burst(tx(at=later(i)), redis_client, cfg) for i in range(4)]
    assert results[:3] == [None, None, None]
    assert results[3] is not None
    assert results[3].details["count_in_window"] == 4


def test_old_entries_fall_out_of_the_window(redis_client, redis_config):
    cfg = replace(redis_config, burst_threshold=3, burst_window_seconds=60)
    for i in range(3):
        check_fraud_burst(tx(at=later(i)), redis_client, cfg)
    # 61s later the first three have aged out; count is 1
    assert check_fraud_burst(tx(at=later(100)), redis_client, cfg) is None


def test_redelivered_record_does_not_double_count(redis_client, redis_config):
    cfg = replace(redis_config, burst_threshold=3)
    same = tx(event_id="dup")
    for _ in range(10):
        assert check_fraud_burst(same, redis_client, cfg) is None


def test_users_are_independent(redis_client, redis_config):
    cfg = replace(redis_config, burst_threshold=1)
    check_fraud_burst(tx(user_id="a"), redis_client, cfg)
    assert check_fraud_burst(tx(user_id="b"), redis_client, cfg) is None
