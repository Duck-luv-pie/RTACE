"""RTWP wire-protocol codec: round trips, integrity, and malformed frames."""

import struct

import pytest

from netgw import protocol as p
from netgw.protocol import Frame, MsgType

KEY = b"shared-secret-key"


def test_round_trip_preserves_every_field():
    f = Frame(type=MsgType.EVENT, payload=b'{"kind":"tx"}', seq=7, timestamp_ms=1_700_000_000_123, flags=0)
    raw = p.encode(f, KEY)
    out, consumed = p.decode(raw)
    assert consumed == len(raw)
    assert out.type is MsgType.EVENT
    assert out.payload == b'{"kind":"tx"}'
    assert out.seq == 7
    assert out.timestamp_ms == 1_700_000_000_123
    assert out.verify(KEY)


def test_empty_payload_round_trips():
    raw = p.encode(Frame(type=MsgType.PING), KEY)
    out, _ = p.decode(raw)
    assert out.type is MsgType.PING and out.payload == b"" and out.verify(KEY)


def test_hmac_detects_a_flipped_payload_bit():
    raw = bytearray(p.encode(Frame(type=MsgType.EVENT, payload=b"hello", seq=1), KEY))
    raw[-1] ^= 0x01  # tamper with the last payload byte
    out, _ = p.decode(bytes(raw))
    assert not out.verify(KEY)
    with pytest.raises(p.BadHmac):
        out.require_valid(KEY)


def test_hmac_detects_a_flipped_header_bit():
    raw = bytearray(p.encode(Frame(type=MsgType.EVENT, payload=b"hello", seq=1), KEY))
    raw[10] ^= 0x01  # flip a byte inside the length/seq region of the header
    # may still decode structurally, but the MAC must fail
    try:
        out, _ = p.decode(bytes(raw))
        assert not out.verify(KEY)
    except p.ProtocolError:
        pass  # a structural check caught it first — also acceptable


def test_wrong_key_fails_verification():
    raw = p.encode(Frame(type=MsgType.EVENT, payload=b"x", seq=1), KEY)
    out, _ = p.decode(raw)
    assert not out.verify(b"a-different-key")


def test_bad_magic_is_rejected():
    raw = bytearray(p.encode(Frame(type=MsgType.EVENT, payload=b"x"), KEY))
    raw[0:2] = b"XX"
    with pytest.raises(p.BadMagic):
        p.decode(bytes(raw))


def test_unsupported_version_is_rejected():
    raw = bytearray(p.encode(Frame(type=MsgType.EVENT, payload=b"x"), KEY))
    raw[2] = 99
    with pytest.raises(p.UnsupportedVersion):
        p.decode(bytes(raw))


def test_unknown_type_is_rejected():
    raw = bytearray(p.encode(Frame(type=MsgType.EVENT, payload=b"x"), KEY))
    raw[3] = 200
    with pytest.raises(p.UnknownType):
        p.decode(bytes(raw))


def test_partial_header_asks_for_more():
    raw = p.encode(Frame(type=MsgType.EVENT, payload=b"x"), KEY)
    with pytest.raises(p.NeedMoreData):
        p.decode(raw[:10])


def test_partial_payload_asks_for_more():
    raw = p.encode(Frame(type=MsgType.EVENT, payload=b"abcdef", seq=1), KEY)
    with pytest.raises(p.NeedMoreData) as ei:
        p.decode(raw[:-2])
    assert ei.value.needed == 2


def test_oversized_declared_length_is_rejected_on_the_header_alone():
    # A hostile header claiming a huge payload must be refused without buffering it.
    header = struct.pack(
        ">2sBBBBIQQ32s", p.MAGIC, p.VERSION, int(MsgType.EVENT), 0, 0,
        p.MAX_PAYLOAD + 1, 1, 0, b"\x00" * 32,
    )
    with pytest.raises(p.FrameTooLarge):
        p.decode(header)


def test_encode_refuses_to_emit_an_oversized_frame():
    with pytest.raises(p.FrameTooLarge):
        p.encode(Frame(type=MsgType.EVENT, payload=b"x" * (p.MAX_PAYLOAD + 1)), KEY)


def test_two_frames_in_one_buffer_decode_in_sequence():
    a = p.encode(Frame(type=MsgType.EVENT, payload=b"one", seq=1), KEY)
    b = p.encode(Frame(type=MsgType.EVENT, payload=b"two", seq=2), KEY)
    buf = a + b
    f1, n1 = p.decode(buf)
    f2, n2 = p.decode(buf[n1:])
    assert f1.payload == b"one" and f2.payload == b"two"
    assert n1 + n2 == len(buf)


def test_auth_proof_matches_on_both_sides():
    nonce = b"random-nonce-1234"
    assert p.hmac_proof(KEY, nonce) == p.hmac_proof(KEY, nonce)
    assert p.hmac_proof(KEY, nonce) != p.hmac_proof(b"other", nonce)
