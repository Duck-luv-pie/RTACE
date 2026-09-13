import itertools
import random
from collections import Counter

from common.models import AuthEvent, TransactionEvent
from simulator.transaction_simulator import (
    BURST_SIZE,
    STUFFING_IP_FAILS,
    STUFFING_USER_FAILS,
    SimulatorSettings,
    _parse_args,
    generate_events,
)


def _take(settings, iterations):
    """Events from the first `iterations` complete iterations (never cuts a scenario in half)."""
    out, seen = [], 0
    for e in generate_events(settings, random.Random(1)):
        if e.scenario == "__sleep__":
            seen += 1
            if seen >= iterations:
                break
            continue
        out.append(e)
    return out


def test_normal_traffic_stays_at_home_city():
    s = SimulatorSettings(num_users=5, replay_probability=0, impossible_travel_probability=0,
                          burst_every=0, stuffing_user_every=0, stuffing_ip_every=0)
    txs = [e.event for e in _take(s, 200) if isinstance(e.event, TransactionEvent)]
    per_user = {}
    for t in txs:
        per_user.setdefault(t.user_id, set()).add(t.location)
    assert all(len(locs) == 1 for locs in per_user.values())


def test_replay_reuses_the_previous_event_verbatim():
    s = SimulatorSettings(num_users=2, replay_probability=1.0, impossible_travel_probability=0,
                          burst_every=0, stuffing_user_every=0, stuffing_ip_every=0)
    events = _take(s, 30)
    replays = [e for e in events if e.scenario == "replay"]
    assert replays
    ids = {e.event.event_id for e in events if isinstance(e.event, TransactionEvent)}
    # every replayed id also appeared as a normal transaction earlier
    normals = {e.event.event_id for e in events if e.scenario == "normal" and isinstance(e.event, TransactionEvent)}
    assert all(r.event.event_id in normals for r in replays)
    assert len(ids) < len([e for e in events if isinstance(e.event, TransactionEvent)])


def test_impossible_travel_leaves_home_and_is_tagged():
    s = SimulatorSettings(num_users=1, replay_probability=0, impossible_travel_probability=1.0,
                          burst_every=0, stuffing_user_every=0, stuffing_ip_every=0)
    first = [e for e in _take(s, 4) if isinstance(e.event, TransactionEvent)]
    assert {e.scenario for e in first} == {"impossible_travel"}


def test_scheduled_scenarios_fire_with_expected_sizes():
    s = SimulatorSettings(num_users=5, replay_probability=0, impossible_travel_probability=0,
                          burst_every=3, stuffing_user_every=4, stuffing_ip_every=5)
    events = _take(s, 60)
    c = Counter(e.scenario for e in events)
    assert c["fraud_burst"] % BURST_SIZE == 0 and c["fraud_burst"] > 0
    assert c["stuffing_user"] % STUFFING_USER_FAILS == 0 and c["stuffing_user"] > 0
    assert c["stuffing_ip"] % STUFFING_IP_FAILS == 0 and c["stuffing_ip"] > 0
    burst_users = {e.event.user_id for e in events if e.scenario == "fraud_burst"}
    assert burst_users  # one victim per burst
    assert all(not e.event.success for e in events if e.scenario.startswith("stuffing"))
    assert all(isinstance(e.event, AuthEvent) for e in events if e.scenario.startswith("stuffing"))


def test_seed_makes_stream_reproducible():
    s = SimulatorSettings(num_users=3, seed=7)
    a = [(e.scenario, e.event.user_id) for e in itertools.islice(generate_events(s), 50) if e.event]
    b = [(e.scenario, e.event.user_id) for e in itertools.islice(generate_events(s), 50) if e.event]
    assert a == b


def test_cli_flags_override_env(monkeypatch):
    monkeypatch.setenv("SIM_USERS", "7")
    monkeypatch.setenv("SIM_BURST_EVERY", "0")
    s = _parse_args(["--replay-p", "0.3"])
    assert s.num_users == 7 and s.burst_every == 0 and s.replay_probability == 0.3
