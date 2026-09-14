"""RTWP — the RTACE Transaction Wire Protocol.

A small, hand-written binary framing protocol spoken over raw TCP. It exists so
transactions and logins enter RTACE the way they would from a real client
network: length-prefixed frames, a versioned header, per-frame authentication
and integrity, monotonic sequence numbers, and freshness timestamps. Everything
the ingest gateway needs to reject spoofed, tampered, replayed, or malformed
traffic lives in the header, before a single byte of payload is trusted.

Wire format (network byte order, big-endian), fixed 58-byte header + payload:

    offset  size  field
    0       2     magic         b"RT"            frame sync / cheap sanity gate
    2       1     version       0x01
    3       1     type          message type (see below)
    4       1     flags         reserved bit-field
    5       1     reserved      0
    6       4     payload_len   uint32, hard-capped at MAX_PAYLOAD
    10      8     seq           uint64, strictly increasing per connection
    18      8     timestamp_ms  uint64, unix millis, checked against a window
    26      32    hmac          HMAC-SHA256(key, header-with-hmac-zeroed || payload)
    58      ...   payload       payload_len bytes

The HMAC covers the whole header (with the hmac field zeroed) and the payload,
so any bit-flip in headers or body is detected, and only a holder of the shared
secret can forge a frame. Sequence numbers make a captured frame a replay the
second time it is seen; the timestamp bounds how long a captured frame stays
useful at all.

This module is pure and transport-agnostic: it turns bytes into frames and
frames into bytes, and nothing else. It performs no I/O.
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass, field
from enum import IntEnum

MAGIC = b"RT"
VERSION = 1

_HEADER_FMT = ">2sBBBBIQQ32s"
HEADER_SIZE = struct.calcsize(_HEADER_FMT)  # 58
assert HEADER_SIZE == 58

# Hard ceiling on a declared payload length. A frame claiming more than this is
# rejected on its header alone, so a hostile peer cannot make the gateway
# allocate a huge buffer (a classic length-prefix memory-exhaustion attack).
MAX_PAYLOAD = 64 * 1024
_ZERO_HMAC = b"\x00" * 32


class MsgType(IntEnum):
    HELLO = 1      # client -> server: payload = client_id (utf-8)
    CHALLENGE = 2  # server -> client: payload = random nonce
    AUTH = 3       # client -> server: payload = HMAC(secret, nonce) proof
    READY = 4      # server -> client: session authenticated
    EVENT = 5      # client -> server: payload = JSON transaction/auth event
    ACK = 6        # server -> client: payload = acked seq (ascii)
    ERROR = 7      # server -> client: payload = "<code> <message>"
    PING = 8
    PONG = 9
    BYE = 10


# ---- flags -----------------------------------------------------------------
FLAG_NONE = 0


# ---- errors ----------------------------------------------------------------
class ProtocolError(Exception):
    """Base for any frame that violates the wire format. Carries a short,
    stable ``code`` used in ERROR frames and metrics labels."""

    code = "protocol_error"


class NeedMoreData(Exception):
    """Not enough bytes buffered yet to decode a full frame. Not a fault —
    the caller reads more from the socket and tries again."""

    def __init__(self, needed: int):
        super().__init__(f"need {needed} more bytes")
        self.needed = needed


class BadMagic(ProtocolError):
    code = "bad_magic"


class UnsupportedVersion(ProtocolError):
    code = "unsupported_version"


class FrameTooLarge(ProtocolError):
    code = "frame_too_large"


class UnknownType(ProtocolError):
    code = "unknown_type"


class BadHmac(ProtocolError):
    code = "bad_hmac"


@dataclass
class Frame:
    type: MsgType
    payload: bytes = b""
    seq: int = 0
    timestamp_ms: int = 0
    flags: int = FLAG_NONE
    # Filled in on decode so verify() can recompute the MAC without re-parsing.
    _signed_region: bytes = field(default=b"", repr=False)
    _hmac: bytes = field(default=b"", repr=False)

    def verify(self, key: bytes) -> bool:
        """True iff the frame's HMAC matches ``key`` over its signed region.

        Uses a constant-time compare so a timing side-channel cannot leak how
        many leading bytes of a forged MAC were correct.
        """
        if not self._hmac:
            return False
        expected = hmac.new(key, self._signed_region, hashlib.sha256).digest()
        return hmac.compare_digest(expected, self._hmac)

    def require_valid(self, key: bytes) -> "Frame":
        if not self.verify(key):
            raise BadHmac("frame HMAC verification failed")
        return self

    def text(self) -> str:
        return self.payload.decode("utf-8", "replace")


def encode(frame: Frame, key: bytes) -> bytes:
    """Serialise a frame and sign it with ``key`` (HMAC-SHA256).

    Raises FrameTooLarge if the payload exceeds MAX_PAYLOAD, so a bug on the
    sending side can't emit a frame the receiver is obligated to reject.
    """
    payload = frame.payload or b""
    if len(payload) > MAX_PAYLOAD:
        raise FrameTooLarge(f"payload {len(payload)} > {MAX_PAYLOAD}")
    header0 = struct.pack(
        _HEADER_FMT, MAGIC, VERSION, int(frame.type), frame.flags & 0xFF, 0,
        len(payload), frame.seq & 0xFFFFFFFFFFFFFFFF, frame.timestamp_ms & 0xFFFFFFFFFFFFFFFF,
        _ZERO_HMAC,
    )
    region = header0 + payload
    mac = hmac.new(key, region, hashlib.sha256).digest()
    return header0[:-32] + mac + payload


def decode(buf: bytes) -> tuple[Frame, int]:
    """Decode one frame from the front of ``buf``.

    Returns (frame, bytes_consumed). Raises NeedMoreData when the buffer holds
    a partial frame (the caller reads more and retries), and a ProtocolError
    subtype when the bytes present are structurally invalid. HMAC is NOT checked
    here — the caller verifies with the key it selects for the connection.
    """
    if len(buf) < HEADER_SIZE:
        raise NeedMoreData(HEADER_SIZE - len(buf))

    magic, version, mtype, flags, _reserved, plen, seq, ts, mac = struct.unpack(
        _HEADER_FMT, buf[:HEADER_SIZE]
    )
    if magic != MAGIC:
        raise BadMagic(f"expected {MAGIC!r}, got {magic!r}")
    if version != VERSION:
        raise UnsupportedVersion(f"version {version} not supported")
    if plen > MAX_PAYLOAD:
        raise FrameTooLarge(f"declared payload {plen} > {MAX_PAYLOAD}")
    try:
        mt = MsgType(mtype)
    except ValueError as e:
        raise UnknownType(f"unknown message type {mtype}") from e

    total = HEADER_SIZE + plen
    if len(buf) < total:
        raise NeedMoreData(total - len(buf))

    payload = buf[HEADER_SIZE:total]
    signed_region = buf[:26] + _ZERO_HMAC + payload  # header (hmac zeroed) + payload
    frame = Frame(type=mt, payload=payload, seq=seq, timestamp_ms=ts, flags=flags,
                  _signed_region=signed_region, _hmac=mac)
    return frame, total


def hmac_proof(secret: bytes, nonce: bytes) -> bytes:
    """The AUTH proof: HMAC-SHA256(secret, nonce). Same on both sides, so the
    server can confirm the client holds the secret without it ever crossing
    the wire."""
    return hmac.new(secret, nonce, hashlib.sha256).digest()
