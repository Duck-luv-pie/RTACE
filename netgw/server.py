"""RTWP ingest gateway: a hand-written TCP server that is RTACE's front door.

Every transaction and login enters here over the wire protocol in netgw.protocol
before it is trusted or forwarded into the Kafka pipeline. The server is where
the network-security controls live:

  * connection management  - global and per-IP caps (connection-exhaustion defence)
  * timeouts               - bounded handshake and idle windows (slowloris defence)
  * authentication         - HELLO / CHALLENGE / AUTH, HMAC challenge-response
  * integrity              - per-frame HMAC verification
  * replay protection      - monotonic sequence numbers + timestamp freshness window
  * malformed handling     - typed protocol errors, a strike system, no huge allocs
  * rate limiting          - per-connection token bucket (flood / rate-abuse defence)
  * shared blocklist       - refuses IPs the fraud engine (or this gateway) has blocked,
                             and blocks abusive IPs into the same Redis key space

The core (GatewayServer) depends only on two small interfaces — a Sink to
forward accepted events and a Blocklist for the shared IP blocklist — so it runs
in tests over loopback with no Kafka or Redis. ``run_gateway`` wires the real
implementations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import ssl
import struct
import time
from typing import Optional, Protocol

from netgw import metrics
from netgw.config import GatewayConfig
from netgw.protocol import (
    HEADER_SIZE,
    MAGIC,
    MAX_PAYLOAD,
    VERSION,
    BadMagic,
    Frame,
    FrameTooLarge,
    MsgType,
    ProtocolError,
    UnsupportedVersion,
    decode,
    encode,
    hmac_proof,
)

logger = logging.getLogger(__name__)


# ---- collaborators ---------------------------------------------------------
class Sink(Protocol):
    def forward(self, kind: str, event: dict) -> None: ...


class Blocklist(Protocol):
    def is_blocked(self, ip: str) -> bool: ...
    def block(self, ip: str, ttl_s: int) -> None: ...


class NullBlocklist:
    def is_blocked(self, ip: str) -> bool: return False
    def block(self, ip: str, ttl_s: int) -> None: pass


class MemorySink:
    """Collects forwarded events in memory (tests / dry runs)."""
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
    def forward(self, kind: str, event: dict) -> None:
        self.events.append((kind, event))


# ---- token bucket ----------------------------------------------------------
class TokenBucket:
    """Classic token bucket: `capacity` burst, refilling `rate` tokens/sec."""
    def __init__(self, capacity: float, rate: float, now: float):
        self.capacity = capacity
        self.rate = rate
        self.tokens = capacity
        self.ts = now

    def take(self, now: float, n: float = 1.0) -> bool:
        self.tokens = min(self.capacity, self.tokens + (now - self.ts) * self.rate)
        self.ts = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


# ---- connection registry (exhaustion defence) ------------------------------
class ConnectionRegistry:
    def __init__(self, max_total: int, max_per_ip: int):
        self.max_total = max_total
        self.max_per_ip = max_per_ip
        self.total = 0
        self.per_ip: dict[str, int] = {}

    def admit(self, ip: str) -> Optional[str]:
        """Return None to admit, or a rejection reason label."""
        if self.total >= self.max_total:
            return "rejected_maxconn"
        if self.per_ip.get(ip, 0) >= self.max_per_ip:
            return "rejected_maxperip"
        self.total += 1
        self.per_ip[ip] = self.per_ip.get(ip, 0) + 1
        return None

    def release(self, ip: str) -> None:
        self.total = max(0, self.total - 1)
        if ip in self.per_ip:
            self.per_ip[ip] -= 1
            if self.per_ip[ip] <= 0:
                del self.per_ip[ip]


class _Abort(Exception):
    """Raised internally to end a connection with a reason label."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class GatewayServer:
    def __init__(self, config: GatewayConfig, sink: Sink, blocklist: Optional[Blocklist] = None):
        self.cfg = config
        self.sink = sink
        self.blocklist = blocklist or NullBlocklist()
        self.registry = ConnectionRegistry(config.max_connections, config.max_conns_per_ip)
        self._server: Optional[asyncio.base_events.Server] = None

    # ---- lifecycle ----
    async def start(self) -> asyncio.base_events.Server:
        ssl_ctx = self._ssl_context()
        self._server = await asyncio.start_server(
            self._on_connect, self.cfg.host, self.cfg.port, ssl=ssl_ctx,
        )
        socks = ", ".join(str(s.getsockname()) for s in self._server.sockets or [])
        logger.info("RTWP gateway listening on %s (tls=%s)", socks, bool(ssl_ctx))
        return self._server

    def _ssl_context(self) -> Optional[ssl.SSLContext]:
        if not self.cfg.tls_enabled:
            return None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cfg.tls_certfile, self.cfg.tls_keyfile)
        if self.cfg.tls_cafile:  # mTLS: require and verify a client certificate
            ctx.load_verify_locations(self.cfg.tls_cafile)
            ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    # ---- per-connection ----
    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "?"

        # 1. Shared blocklist: refuse IPs the fraud engine or this gateway blocked.
        if self.blocklist.is_blocked(ip):
            metrics.connections_total.labels(result="rejected_ipblock").inc()
            writer.close()
            return

        # 2. Connection-exhaustion caps.
        reason = self.registry.admit(ip)
        if reason:
            metrics.connections_total.labels(result=reason).inc()
            await self._safe_error(writer, "conn_limit", reason, key=None)
            writer.close()
            return

        metrics.connections_total.labels(result="accepted").inc()
        metrics.connections_active.inc()
        try:
            key = self.cfg.shared_secret
            await self._handshake(reader, writer, key, ip)
            await self._session(reader, writer, key, ip)
        except asyncio.IncompleteReadError:
            pass  # peer closed
        except asyncio.TimeoutError:
            pass  # timeout already counted where it happened
        except _Abort as a:
            logger.info("connection from %s aborted: %s", ip, a.reason)
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:  # noqa: BLE001 - never let one connection crash the server
            logger.exception("unexpected error on connection from %s: %s", ip, e)
        finally:
            self.registry.release(ip)
            metrics.connections_active.dec()
            try:
                writer.close()
            except Exception:
                pass

    async def _read_frame(self, reader: asyncio.StreamReader, timeout: float) -> Frame:
        """Read exactly one framed message, bounding the payload before allocating."""
        header = await asyncio.wait_for(reader.readexactly(HEADER_SIZE), timeout)
        if header[0:2] != MAGIC:
            raise BadMagic("bad magic")
        if header[2] != VERSION:
            raise UnsupportedVersion(f"version {header[2]}")
        plen = struct.unpack(">I", header[6:10])[0]
        if plen > MAX_PAYLOAD:
            raise FrameTooLarge(f"declared payload {plen}")
        payload = await asyncio.wait_for(reader.readexactly(plen), timeout) if plen else b""
        metrics.bytes_received_total.inc(HEADER_SIZE + plen)
        frame, _ = decode(header + payload)
        return frame

    async def _handshake(self, reader, writer, key: bytes, ip: str) -> None:
        t0 = time.perf_counter()
        try:
            hello = await self._read_frame(reader, self.cfg.handshake_timeout_s)
        except asyncio.TimeoutError:
            metrics.timeouts_total.labels(phase="handshake").inc()
            raise
        if hello.type is not MsgType.HELLO or not hello.verify(key):
            metrics.auth_total.labels(result="fail").inc()
            metrics.frames_total.labels(type="HELLO", result="bad_hmac").inc()
            await self._safe_error(writer, "auth_failed", "expected authenticated HELLO", key)
            raise _Abort("bad_hello")

        client_id = hello.text()[:64]
        nonce = secrets.token_bytes(16)
        await self._send(writer, Frame(type=MsgType.CHALLENGE, payload=nonce, seq=0), key)

        try:
            auth = await self._read_frame(reader, self.cfg.handshake_timeout_s)
        except asyncio.TimeoutError:
            metrics.timeouts_total.labels(phase="handshake").inc()
            raise
        expected = hmac_proof(key, nonce)
        if auth.type is not MsgType.AUTH or not auth.verify(key) or not secrets.compare_digest(auth.payload, expected):
            metrics.auth_total.labels(result="fail").inc()
            await self._safe_error(writer, "auth_failed", "challenge response invalid", key)
            raise _Abort("auth_failed")

        metrics.auth_total.labels(result="ok").inc()
        metrics.handshake_seconds.observe(time.perf_counter() - t0)
        await self._send(writer, Frame(type=MsgType.READY, seq=0), key)
        logger.info("client %r authenticated from %s", client_id, ip)

    async def _session(self, reader, writer, key: bytes, ip: str) -> None:
        bucket = TokenBucket(self.cfg.rate_capacity, self.cfg.rate_refill_per_s, time.monotonic())
        last_seq = -1
        strikes = 0

        async def strike(reason: str, code: str, msg: str) -> None:
            nonlocal strikes
            strikes += 1
            await self._safe_error(writer, code, msg, key)
            if strikes >= self.cfg.max_strikes:
                self.blocklist.block(ip, self.cfg.block_ttl_s)
                metrics.abuse_blocks_total.inc()
                logger.warning("blocking %s for abuse (%s), %d strikes", ip, reason, strikes)
                raise _Abort(f"blocked:{reason}")

        while True:
            try:
                frame = await self._read_frame(reader, self.cfg.idle_timeout_s)
            except asyncio.TimeoutError:
                metrics.timeouts_total.labels(phase="idle").inc()
                raise
            except (BadMagic, UnsupportedVersion, FrameTooLarge, ProtocolError) as e:
                metrics.malformed_total.labels(reason=getattr(e, "code", "protocol_error")).inc()
                metrics.frames_total.labels(type="?", result="malformed").inc()
                await strike("malformed", getattr(e, "code", "protocol_error"), str(e))
                continue

            tname = frame.type.name

            if frame.type is MsgType.PING:
                await self._send(writer, Frame(type=MsgType.PONG, seq=0), key)
                continue
            if frame.type is MsgType.BYE:
                return

            if frame.type is not MsgType.EVENT:
                metrics.frames_total.labels(type=tname, result="unauthorized").inc()
                await strike("unexpected_type", "unexpected_type", f"{tname} not allowed in session")
                continue

            # integrity + authentication
            if not frame.verify(key):
                metrics.frames_total.labels(type=tname, result="bad_hmac").inc()
                await strike("bad_hmac", "bad_hmac", "frame HMAC invalid")
                continue

            # replay: monotonic sequence
            if frame.seq <= last_seq:
                metrics.frames_total.labels(type=tname, result="replay").inc()
                metrics.replays_rejected_total.labels(reason="seq_regression").inc()
                await strike("replay_seq", "replay", f"seq {frame.seq} <= {last_seq}")
                continue

            # replay: timestamp freshness
            now_ms = int(time.time() * 1000)
            if abs(now_ms - frame.timestamp_ms) > self.cfg.replay_window_s * 1000:
                metrics.frames_total.labels(type=tname, result="replay").inc()
                metrics.replays_rejected_total.labels(reason="stale").inc()
                await strike("replay_stale", "replay", "timestamp outside freshness window")
                continue

            # rate limiting (flood defence)
            if not bucket.take(time.monotonic()):
                metrics.frames_total.labels(type=tname, result="rate_limited").inc()
                metrics.rate_limited_total.inc()
                await strike("rate_limited", "rate_limited", "slow down")
                continue

            # payload must be a valid event
            kind = self._forward_event(frame)
            if kind is None:
                metrics.malformed_total.labels(reason="bad_payload").inc()
                metrics.frames_total.labels(type=tname, result="malformed").inc()
                await strike("bad_payload", "bad_payload", "event payload invalid")
                continue

            last_seq = frame.seq
            metrics.frames_total.labels(type=tname, result="ok").inc()
            metrics.events_forwarded_total.labels(kind=kind).inc()
            await self._send(writer, Frame(type=MsgType.ACK, payload=str(frame.seq).encode(), seq=0), key)

    def _forward_event(self, frame: Frame) -> Optional[str]:
        """Validate the payload against the RTACE schemas and forward it.
        Returns the kind ('tx'|'auth') on success, or None if invalid."""
        try:
            data = json.loads(frame.payload.decode("utf-8"))
            kind = data.get("kind")
        except (ValueError, AttributeError):
            return None
        # Validate with the same models the pipeline uses (deferred import keeps
        # the protocol/server importable without pydantic in minimal contexts).
        from common.models import AuthEvent, TransactionEvent
        try:
            if kind == "tx":
                ev = TransactionEvent.model_validate({k: v for k, v in data.items() if k != "kind"})
                self.sink.forward("tx", ev.model_dump(mode="json"))
                return "tx"
            if kind == "auth":
                ev = AuthEvent.model_validate({k: v for k, v in data.items() if k != "kind"})
                self.sink.forward("auth", ev.model_dump(mode="json"))
                return "auth"
        except Exception:  # noqa: BLE001 - any validation failure is a bad payload
            return None
        return None

    async def _send(self, writer: asyncio.StreamWriter, frame: Frame, key: bytes) -> None:
        frame.timestamp_ms = frame.timestamp_ms or int(time.time() * 1000)
        writer.write(encode(frame, key))
        await writer.drain()

    async def _safe_error(self, writer, code: str, msg: str, key: Optional[bytes]) -> None:
        # Sign error frames when we have a key; pre-auth errors go unsigned.
        try:
            body = f"{code} {msg}".encode()[:200]
            writer.write(encode(Frame(type=MsgType.ERROR, payload=body, seq=0), key or b""))
            await writer.drain()
        except Exception:
            pass


# ---- production wiring ------------------------------------------------------
class KafkaSink:
    def __init__(self):
        from common.kafka_client import create_producer, send_message
        from configs.kafka_config import KafkaConfig
        self._cfg = KafkaConfig.from_env()
        self._producer = create_producer(self._cfg)
        self._send = send_message

    def forward(self, kind: str, event: dict) -> None:
        topic = self._cfg.tx_events_topic if kind == "tx" else self._cfg.auth_events_topic
        self._send(self._producer, topic, event, key=event.get("user_id"))


class RedisBlocklist:
    """Shared IP blocklist over the same Redis keys the fraud containment uses,
    so a network-abusive IP and a fraud-abusive IP are blocked in one place."""
    def __init__(self):
        from common.redis_client import create_redis_client
        from configs.redis_config import RedisConfig
        self._r = create_redis_client(RedisConfig.from_env())

    def is_blocked(self, ip: str) -> bool:
        from common.enforcement import is_ip_blocked
        try:
            return is_ip_blocked(self._r, ip)
        except Exception:  # noqa: BLE001 - never fail-closed the whole gateway on a Redis blip here
            return False

    def block(self, ip: str, ttl_s: int) -> None:
        from common.enforcement import ip_block_key
        try:
            self._r.setex(ip_block_key(ip), ttl_s, "netgw:abuse")
        except Exception:  # noqa: BLE001
            logger.warning("could not persist block for %s", ip)


async def _serve(config: GatewayConfig) -> None:
    sink = KafkaSink() if config.kafka_enabled else MemorySink()
    blocklist = RedisBlocklist() if config.redis_enabled else NullBlocklist()
    server = GatewayServer(config, sink, blocklist)
    srv = await server.start()
    async with srv:
        await srv.serve_forever()


def run_gateway() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    from prometheus_client import start_http_server
    cfg = GatewayConfig.from_env()
    start_http_server(cfg.metrics_port)
    logger.info("gateway metrics on :%d", cfg.metrics_port)
    try:
        asyncio.run(_serve(cfg))
    except KeyboardInterrupt:
        logger.info("gateway shutting down")


if __name__ == "__main__":
    run_gateway()
