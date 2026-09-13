from tests.factories import later, tx


def test_fires_only_when_count_exceeds_threshold(make_pipeline):
    p, out, _ = make_pipeline(burst_threshold=3)
    results = [p.handle_transaction(tx(at=later(i)), f"r{i}") for i in range(4)]
    assert results[:3] == [[], [], []]
    (det,) = results[3]
    assert det.detection_type == "fraud_burst" and det.details["count_in_window"] == 4


def test_old_entries_fall_out_of_the_window(make_pipeline):
    p, out, _ = make_pipeline(burst_threshold=3, burst_window_seconds=60)
    for i in range(3):
        p.handle_transaction(tx(at=later(i)), f"r{i}")
    assert p.handle_transaction(tx(at=later(100)), "r100") == []


def test_redelivered_record_does_not_double_count(make_pipeline, redis_client):
    p, out, _ = make_pipeline(burst_threshold=3)
    same = tx(event_id="dup")
    for _ in range(10):
        assert p.handle_transaction(same, "same-ref") == []
    assert redis_client.zcard("burst:user_1:1m") == 1


def test_users_are_independent(make_pipeline):
    p, out, _ = make_pipeline(burst_threshold=1)
    p.handle_transaction(tx(user_id="a"), "r1")
    assert p.handle_transaction(tx(user_id="b"), "r2") == []


def test_burst_key_expires(make_pipeline, redis_client):
    p, out, _ = make_pipeline(burst_key_ttl_seconds=90)
    p.handle_transaction(tx(), "r1")
    assert 0 < redis_client.ttl("burst:user_1:1m") <= 90
