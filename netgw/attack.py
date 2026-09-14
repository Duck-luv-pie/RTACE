"""Adversarial traffic generator for the RTWP gateway.

A red-team client that exercises each attack class the gateway defends against
and reports what the defences did. Useful as a demo ("watch the gateway refuse
this") and as a smoke test against a running instance. It never needs the
secret to *misbehave* — several modes are pre-authentication.

    python -m netgw.attack --mode all --host 127.0.0.1 --port 9500

Modes: normal, flood, replay, tamper, malformed, exhaust, stuff, all.
"""

from __future__ import annotations

import argparse
import socket
import time

from netgw import protocol as p
from netgw.client import AuthFailed, RTWPClient, RTWPError, ServerError
from netgw.protocol import Frame, MsgType, encode


def _tx(i: int) -> dict:
    return {"event_id": f"atk-{i}-{time.time_ns()}", "user_id": "user_1", "amount": 10.0 + i,
            "merchant": "Amazon", "timestamp": "2026-01-01T00:00:00+00:00",
            "location": "US-CA", "latitude": 37.0, "longitude": -122.0}


def normal(host, port, secret, count=20, **_):
    c = RTWPClient(host, port, "red-normal", secret).connect()
    ok = 0
    for i in range(count):
        try:
            if c.send_event("tx", _tx(i)).type is MsgType.ACK:
                ok += 1
        except ServerError:
            pass
    c.close()
    return f"normal: {ok}/{count} events accepted (baseline — this is legitimate traffic)"


def flood(host, port, secret, count=200, **_):
    """Send as fast as possible; the token-bucket rate limiter should throttle."""
    c = RTWPClient(host, port, "red-flood", secret).connect()
    ok = limited = 0
    for i in range(count):
        try:
            c.send_event("tx", _tx(i))
            ok += 1
        except ServerError as e:
            if "rate_limited" in str(e):
                limited += 1
        except RTWPError:
            break  # blocked/disconnected after too much abuse
    c._reset()
    return f"flood: {ok} accepted, {limited} rate-limited, then connection dropped — flood contained"


def replay(host, port, secret, **_):
    """Capture a valid frame and resend it verbatim — must be rejected."""
    c = RTWPClient(host, port, "red-replay", secret).connect()
    import json
    payload = json.dumps({"kind": "tx", **_tx(1)}).encode()
    seq = c._next()
    frame = encode(Frame(type=MsgType.EVENT, payload=payload, seq=seq, timestamp_ms=int(time.time()*1000)), secret)
    c.send_raw(frame); first = c._read()
    c.send_raw(frame); second = c._read()  # exact same bytes again
    c._reset()
    return (f"replay: first={first.type.name}, resend={second.type.name} "
            f"({second.text() if second.type is MsgType.ERROR else 'ACCEPTED — BUG'}) — replay rejected")


def tamper(host, port, secret, **_):
    """Flip a payload byte after signing — HMAC must catch it."""
    c = RTWPClient(host, port, "red-tamper", secret).connect()
    raw = bytearray(encode(Frame(type=MsgType.EVENT, payload=b'{"kind":"tx"}', seq=c._next(),
                                 timestamp_ms=int(time.time()*1000)), secret))
    raw[-1] ^= 0xFF
    c.send_raw(bytes(raw)); reply = c._read()
    c._reset()
    return f"tamper: server said {reply.type.name} ({reply.text() if reply.type is MsgType.ERROR else '?'}) — integrity enforced"


def malformed(host, port, secret, **_):
    """Pre-auth garbage on a raw socket — the gateway must not choke."""
    results = []
    for label, data in [
        ("random bytes", b"\xde\xad\xbe\xef" * 20),
        ("bad magic", b"XX" + bytes(56)),
        ("oversized length", __import__("struct").pack(
            ">2sBBBBIQQ32s", p.MAGIC, p.VERSION, int(MsgType.EVENT), 0, 0, p.MAX_PAYLOAD + 1, 1, 0, b"\x00"*32)),
    ]:
        try:
            s = socket.create_connection((host, port), timeout=3)
            s.sendall(data)
            s.settimeout(2)
            try:
                resp = s.recv(256)
                results.append(f"{label}→{'ERROR' if resp else 'closed'}")
            except socket.timeout:
                results.append(f"{label}→ignored")
            s.close()
        except OSError:
            results.append(f"{label}→refused")
    return "malformed: " + ", ".join(results) + " — server stayed up"


def exhaust(host, port, secret, count=40, **_):
    """Open many connections and hold them; the per-IP cap should start refusing."""
    held, refused = [], 0
    for _i in range(count):
        try:
            c = RTWPClient(host, port, "red-exhaust", secret, timeout=2).connect()
            held.append(c)
        except (AuthFailed, RTWPError):
            refused += 1
    n = len(held)
    for c in held:
        c._reset()
    return f"exhaust: {n} connections held, {refused} refused by the per-IP cap — exhaustion contained"


def stuff(host, port, secret, count=15, **_):
    """Credential stuffing at the protocol layer: many wrong-secret handshakes."""
    fails = blocked = 0
    for _i in range(count):
        try:
            RTWPClient(host, port, "red-stuff", b"wrong-secret", timeout=2).connect()
        except AuthFailed:
            fails += 1
        except RTWPError:
            blocked += 1
    return f"stuff: {fails} auth failures, {blocked} connections refused (IP blocked after strikes) — auth abuse contained"


MODES = {"normal": normal, "flood": flood, "replay": replay, "tamper": tamper,
         "malformed": malformed, "exhaust": exhaust, "stuff": stuff}


def main(argv=None):
    ap = argparse.ArgumentParser(description="RTWP adversarial traffic generator")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9500)
    ap.add_argument("--secret", default="rtace-dev-secret")
    ap.add_argument("--mode", default="all", choices=[*MODES, "all"])
    ap.add_argument("--count", type=int, default=None)
    a = ap.parse_args(argv)

    secret = a.secret.encode()
    modes = list(MODES) if a.mode == "all" else [a.mode]
    print(f"== RTWP attack run against {a.host}:{a.port} ==")
    for m in modes:
        kw = {"count": a.count} if a.count else {}
        try:
            print(" •", MODES[m](a.host, a.port, secret, **kw))
        except Exception as e:  # noqa: BLE001 - report and continue
            print(f" • {m}: error — {e}")


if __name__ == "__main__":
    main()
