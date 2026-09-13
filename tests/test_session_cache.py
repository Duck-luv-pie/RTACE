from cachetools import TTLCache

from common import session_cache
from tests.factories import T0


def _install(ttl: int, clock):
    session_cache._cache = TTLCache(maxsize=10, ttl=ttl, timer=clock)


def test_read_populates_l1_and_second_read_skips_redis(redis_client):
    session_cache.configure(10, 300)
    session_cache.set_session(redis_client, "u", 1.0, 2.0, T0, 60)
    redis_client.delete("session:u")  # Redis copy gone; L1 must still answer
    assert session_cache.get_session(redis_client, "u")["last_latitude"] == 1.0


def test_clear_forces_redis_read(redis_client):
    session_cache.configure(10, 300)
    session_cache.set_session(redis_client, "u", 1.0, 2.0, T0, 60)
    redis_client.hset("session:u", "last_latitude", "9.0")
    session_cache.clear()
    assert session_cache.get_session(redis_client, "u")["last_latitude"] == 9.0


def test_l1_entries_expire(redis_client):
    now = [0.0]
    _install(ttl=5, clock=lambda: now[0])
    session_cache.set_session(redis_client, "u", 1.0, 2.0, T0, 60)
    redis_client.hset("session:u", "last_latitude", "9.0")
    now[0] = 6.0
    assert session_cache.get_session(redis_client, "u")["last_latitude"] == 9.0
    session_cache.configure(10, 300)


def test_redis_write_sets_ttl(redis_client):
    session_cache.configure(10, 300)
    session_cache.set_session(redis_client, "u", 1.0, 2.0, T0, 123)
    assert 0 < redis_client.ttl("session:u") <= 123
