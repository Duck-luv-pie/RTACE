"""A well-behaved RTWP client over raw TCP sockets.

Uses the low-level ``socket`` API directly (not asyncio) to show the client side
of the protocol end to end: connect, authenticate with the challenge-response
handshake, frame and sign every message, honour timeouts, and reconnect with
backoff. The simulator uses this to deliver events to the gateway; the tests use
it to drive the server; the attack tool builds on it (and deliberately breaks
the rules).
"""

from __future__ import annotations

import socket
import ssl
import time
from typing import Optional

from netgw.protocol import (
    Frame,
    MsgType,
    ProtocolError,
    decode,
    encode,
    hmac_proof,
)


class RTWPError(Exception):
    pass


class AuthFailed(RTWPError):
    pass


class ServerError(RTWPError):
    """The server replied with an ERROR frame."""


class RTWPClient:
    def __init__(self, host: str, port: int, client_id: str, secret: bytes,
                 timeout: float = 5.0, tls: Optional[ssl.SSLContext] = None):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.key = secret
        self.timeout = timeout
        self.tls = tls
        self.sock: Optional[socket.socket] = None
        self._buf = b""
        self._seq = 0

    # ---- connection ----
    def connect(self) -> "RTWPClient":
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self.tls:
            s = self.tls.wrap_socket(s, server_hostname=self.host)
        self.sock = s
        self._buf = b""
        self._handshake()
        return self

    def _handshake(self) -> None:
        # HELLO (authenticated so the server can bind client_id to the secret)
        self._write(Frame(type=MsgType.HELLO, payload=self.client_id.encode(), seq=self._next()))
        challenge = self._read()
        if challenge.type is MsgType.ERROR:
            raise AuthFailed(challenge.text())
        if challenge.type is not MsgType.CHALLENGE or not challenge.verify(self.key):
            raise AuthFailed("no valid challenge from server")
        proof = hmac_proof(self.key, challenge.payload)
        self._write(Frame(type=MsgType.AUTH, payload=proof, seq=self._next()))
        ready = self._read()
        if ready.type is MsgType.ERROR:
            raise AuthFailed(ready.text())
        if ready.type is not MsgType.READY:
            raise AuthFailed(f"expected READY, got {ready.type.name}")

    def close(self) -> None:
        if self.sock:
            try:
                self._write(Frame(type=MsgType.BYE, seq=self._next()))
            except OSError:
                pass
            try:
                self.sock.close()
            finally:
                self.sock = None

    # ---- sending events ----
    def send_event(self, kind: str, event: dict, expect_ack: bool = True) -> Optional[Frame]:
        import json
        payload = json.dumps({"kind": kind, **event}).encode()
        self._write(Frame(type=MsgType.EVENT, payload=payload, seq=self._next(),
                          timestamp_ms=int(time.time() * 1000)))
        if not expect_ack:
            return None
        reply = self._read()
        if reply.type is MsgType.ERROR:
            raise ServerError(reply.text())
        return reply

    def send_event_with_retry(self, kind: str, event: dict, attempts: int = 3, backoff: float = 0.2) -> Optional[Frame]:
        """Deliver an event, reconnecting on a broken connection. Connection
        management and retries the way a real client faces a flaky network."""
        last: Optional[Exception] = None
        for i in range(attempts):
            try:
                if self.sock is None:
                    self.connect()
                return self.send_event(kind, event)
            except (OSError, RTWPError, ProtocolError) as e:
                last = e
                self._reset()
                time.sleep(backoff * (2 ** i))
        raise RTWPError(f"delivery failed after {attempts} attempts: {last}")

    def ping(self) -> bool:
        self._write(Frame(type=MsgType.PING, seq=self._next()))
        return self._read().type is MsgType.PONG

    # ---- framing over the socket ----
    def _write(self, frame: Frame) -> None:
        if self.sock is None:
            raise RTWPError("not connected")
        try:
            self.sock.sendall(encode(frame, self.key))
        except OSError as e:
            raise RTWPError(f"send failed: {e}") from e

    def send_raw(self, data: bytes) -> None:
        """Escape hatch for the attack tool: put arbitrary bytes on the wire."""
        if self.sock is None:
            raise RTWPError("not connected")
        try:
            self.sock.sendall(data)
        except OSError as e:
            raise RTWPError(f"send failed: {e}") from e

    def _read(self) -> Frame:
        """Read one frame, buffering partial reads from the socket."""
        while True:
            try:
                frame, consumed = decode(self._buf)
                self._buf = self._buf[consumed:]
                return frame
            except ProtocolError:
                raise
            except Exception:
                pass  # NeedMoreData — read more below
            try:
                chunk = self.sock.recv(4096)
            except OSError as e:
                raise RTWPError(f"connection error: {e}") from e
            if not chunk:
                raise RTWPError("connection closed by server")
            self._buf += chunk

    def _reset(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self._buf = b""

    def _next(self) -> int:
        self._seq += 1
        return self._seq
