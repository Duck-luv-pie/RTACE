"""Integration tests for the RTWP gateway over real loopback TCP sockets.

The server runs in a background asyncio loop; the tests drive it with the raw
socket client and with hand-built frames, exercising the handshake, integrity,
replay, malformed, rate-limit, connection-cap and blocklist defences end to end.
No Kafka or Redis: a MemorySink and a fake blocklist are injected directly.
"""

import asyncio
import threading
import time

import pytest

from netgw.client import AuthFailed, RTWPClient, RTWPError, ServerError
from netgw.config import GatewayConfig
from netgw.protocol import Frame, MsgType, encode
from netgw.server import GatewayServer, MemorySink

SECRET = b"test-secret"


def cfg(**ov):
    base = dict(
        host="127.0.0.1", port=0, shared_secret=SECRET,
        max_connections=50, max_conns_per_ip=8,
        handshake_timeout_s=2.0, idle_timeout_s=5.0,
        rate_capacity=1000, rate_refill_per_s=1000.0,
        replay_window_s=30, max_strikes=3, block_ttl_s=60,
        tls_enabled=False, tls_certfile="", tls_keyfile="", tls_cafile="",
        metrics_port=0, kafka_enabled=False, redis_enabled=False,
    )
    base.update(ov)
    return GatewayConfig(**base)


class FakeBlocklist:
    def __init__(self, blocked=None):
        self.blocked = set(blocked or [])
        self.block_calls = []
    def is_blocked(self, ip):
        return ip in self.blocked
    def block(self, ip, ttl_s):
        self.blocked.add(ip)
        self.block_calls.append((ip, ttl_s))


class Harness:
    def __init__(self, config, sink, blocklist):
        self.sink = sink
        self.blocklist = blocklist
        self.loop = asyncio.new_event_loop()
        self.gw = GatewayServer(config, sink, blocklist)
        self.port = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(5), "gateway did not start"

    def _run(self):
        asyncio.set_event_loop(self.loop)
        srv = self.loop.run_until_complete(self.gw.start())
        self.port = srv.sockets[0].getsockname()[1]
        self._srv = srv
        self._ready.set()
        self.loop.run_forever()

    def client(self, secret=SECRET, client_id="tester", timeout=3.0):
        return RTWPClient("127.0.0.1", self.port, client_id, secret, timeout=timeout)

    def stop(self):
        def _close():
            self._srv.close()
            for t in asyncio.all_tasks(self.loop):
                t.cancel()
            self.loop.stop()
        self.loop.call_soon_threadsafe(_close)


@pytest.fixture
def make_gateway():
    made = []
    def _make(sink=None, blocklist=None, **ov):
        h = Harness(cfg(**ov), sink or MemorySink(), blocklist or FakeBlocklist())
        made.append(h)
        return h
    yield _make
    for h in made:
        h.stop()


def _tx(user="user_1", amount=42.5):
    return {"event_id": f"e-{time.time_ns()}", "user_id": user, "amount": amount,
            "merchant": "Amazon", "timestamp": "2026-01-01T00:00:00+00:00",
            "location": "US-CA", "latitude": 37.0, "longitude": -122.0}


def _auth(user="user_1", ip="198.51.100.9", success=False):
    return {"event_id": f"a-{time.time_ns()}", "user_id": user, "ip_address": ip,
            "success": success, "timestamp": "2026-01-01T00:00:00+00:00"}


# ---- happy path ----
def test_authenticated_client_delivers_events(make_gateway):
    gw = make_gateway()
    c = gw.client().connect()
    try:
        ack = c.send_event("tx", _tx())
        assert ack.type is MsgType.ACK
        c.send_event("auth", _auth())
    finally:
        c.close()
    kinds = [k for k, _ in gw.sink.events]
    assert kinds == ["tx", "auth"]
    assert gw.sink.events[0][1]["user_id"] == "user_1"


def test_ping_pong(make_gateway):
    gw = make_gateway()
    c = gw.client().connect()
    try:
        assert c.ping() is True
    finally:
        c.close()


# ---- authentication ----
def test_wrong_secret_is_rejected_at_handshake(make_gateway):
    gw = make_gateway()
    c = gw.client(secret=b"not-the-secret")
    with pytest.raises(AuthFailed):
        c.connect()
    assert gw.sink.events == []


def test_forged_auth_proof_is_rejected(make_gateway):
    """A client with the right HELLO key but a bad AUTH proof cannot pass."""
    gw = make_gateway()
    # Simulate by tampering: connect manually and send a wrong AUTH proof.
    import socket
    from netgw import protocol as p
    s = socket.create_connection(("127.0.0.1", gw.port), timeout=3)
    s.sendall(encode(Frame(type=MsgType.HELLO, payload=b"tester", seq=1), SECRET))
    buf = s.recv(4096)
    challenge, _ = p.decode(buf)
    assert challenge.type is MsgType.CHALLENGE
    s.sendall(encode(Frame(type=MsgType.AUTH, payload=b"wrong-proof", seq=2), SECRET))
    reply, _ = p.decode(s.recv(4096))
    assert reply.type is MsgType.ERROR and "auth" in reply.text()
    s.close()


# ---- replay protection ----
def test_replayed_frame_by_sequence_is_rejected(make_gateway):
    gw = make_gateway()
    c = gw.client().connect()
    try:
        c.send_event("tx", _tx())  # advances server's last_seq
        used_seq = c._seq          # the seq just used
        # Resend a fresh, well-formed frame but reuse the old sequence number.
        frame = Frame(type=MsgType.EVENT, payload=b'{"kind":"tx","event_id":"x","user_id":"u",'
                      b'"amount":1,"merchant":"Amazon","timestamp":"2026-01-01T00:00:00+00:00",'
                      b'"location":"US-CA","latitude":37.0,"longitude":-122.0}',
                      seq=used_seq, timestamp_ms=int(time.time() * 1000))
        c.send_raw(encode(frame, c.key))
        reply = c._read()
        assert reply.type is MsgType.ERROR and "replay" in reply.text()
    finally:
        c._reset()


def test_stale_timestamp_is_rejected_as_replay(make_gateway):
    gw = make_gateway(replay_window_s=10)
    c = gw.client().connect()
    try:
        old = int(time.time() * 1000) - 60_000  # 60s old, window is 10s
        frame = Frame(type=MsgType.EVENT, payload=b'{"kind":"tx","event_id":"x","user_id":"u",'
                      b'"amount":1,"merchant":"Amazon","timestamp":"2026-01-01T00:00:00+00:00",'
                      b'"location":"US-CA","latitude":37.0,"longitude":-122.0}',
                      seq=999, timestamp_ms=old)
        c.send_raw(encode(frame, c.key))
        reply = c._read()
        assert reply.type is MsgType.ERROR and "replay" in reply.text()
    finally:
        c._reset()


# ---- integrity ----
def test_tampered_frame_fails_hmac(make_gateway):
    gw = make_gateway()
    c = gw.client().connect()
    try:
        raw = bytearray(encode(Frame(type=MsgType.EVENT, payload=b'{"kind":"tx"}', seq=500,
                                     timestamp_ms=int(time.time()*1000)), c.key))
        raw[-1] ^= 0xFF  # flip a payload byte after signing
        c.send_raw(bytes(raw))
        reply = c._read()
        assert reply.type is MsgType.ERROR and "hmac" in reply.text()
    finally:
        c._reset()


# ---- malformed handling + abuse blocking ----
def test_malformed_frames_strike_then_block_the_ip(make_gateway):
    bl = FakeBlocklist()
    gw = make_gateway(blocklist=bl, max_strikes=3)
    c = gw.client().connect()
    try:
        # Send garbage with a valid-length header but bad magic, repeatedly.
        garbage = b"XX" + bytes(56)  # 58 bytes, wrong magic
        for _ in range(4):
            try:
                c.send_raw(garbage)
                c._read()
            except RTWPError:
                break
    finally:
        c._reset()
    # after max_strikes the server blocked our IP
    assert ("127.0.0.1", 60) in bl.block_calls


def test_oversized_declared_length_is_refused(make_gateway):
    import struct
    from netgw import protocol as p
    gw = make_gateway()
    c = gw.client().connect()
    try:
        # a header claiming a payload far over MAX_PAYLOAD, no body sent
        header = struct.pack(">2sBBBBIQQ32s", p.MAGIC, p.VERSION, int(MsgType.EVENT), 0, 0,
                             p.MAX_PAYLOAD + 100, 5, int(time.time()*1000), b"\x00"*32)
        c.send_raw(header)
        reply = c._read()
        assert reply.type is MsgType.ERROR
    finally:
        c._reset()


# ---- rate limiting (flood defence) ----
def test_flood_is_rate_limited(make_gateway):
    gw = make_gateway(rate_capacity=3, rate_refill_per_s=0.0, max_strikes=100)
    c = gw.client().connect()
    limited = 0
    try:
        for _ in range(10):
            try:
                c.send_event("tx", _tx())
            except ServerError as e:
                if "rate_limited" in str(e):
                    limited += 1
    finally:
        c._reset()
    assert limited >= 1  # burst of 3 allowed, the rest throttled


# ---- connection exhaustion defence ----
def test_per_ip_connection_cap(make_gateway):
    gw = make_gateway(max_conns_per_ip=2)
    a = gw.client().connect()
    b = gw.client().connect()
    try:
        with pytest.raises((AuthFailed, RTWPError)):
            gw.client(timeout=2).connect()  # third from same IP refused
    finally:
        a.close(); b.close()


# ---- shared blocklist ----
def test_blocked_ip_cannot_connect(make_gateway):
    bl = FakeBlocklist(blocked={"127.0.0.1"})
    gw = make_gateway(blocklist=bl)
    with pytest.raises(RTWPError):
        gw.client(timeout=2).connect()
    assert gw.sink.events == []


# ---- TLS (skipped when openssl is unavailable) ----
import shutil  # noqa: E402
import ssl     # noqa: E402
import subprocess  # noqa: E402


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not available")
def test_tls_handshake_and_event_over_encrypted_channel(make_gateway, tmp_path):
    subprocess.run(["scripts/gen_certs.sh", str(tmp_path)], check=True,
                   capture_output=True, cwd=".")
    gw = make_gateway(
        tls_enabled=True,
        tls_certfile=str(tmp_path / "server.crt"),
        tls_keyfile=str(tmp_path / "server.key"),
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # self-signed in the test; the point is the encrypted channel works
    c = RTWPClient("127.0.0.1", gw.port, "tls-tester", SECRET, timeout=5, tls=ctx)
    c.connect()
    try:
        assert c.send_event("tx", _tx()).type is MsgType.ACK
    finally:
        c.close()
    assert gw.sink.events and gw.sink.events[0][0] == "tx"
