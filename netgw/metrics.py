"""Prometheus metrics for the RTWP gateway — the network-layer traffic analysis.

These are deliberately shaped for spotting malicious traffic: connection
outcomes by reason, frames by type and verdict, and dedicated counters for the
attack classes the gateway defends against (malformed, replay, flood, auth
abuse, connection exhaustion).
"""

from prometheus_client import Counter, Gauge, Histogram, REGISTRY

connections_total = Counter(
    "gw_connections_total",
    "TCP connections seen, by outcome",
    ["result"],  # accepted | rejected_maxconn | rejected_maxperip | rejected_ipblock | rejected_tls
    registry=REGISTRY,
)
connections_active = Gauge(
    "gw_connections_active", "Currently open connections", registry=REGISTRY,
)
auth_total = Counter(
    "gw_auth_total", "Handshake authentication outcomes", ["result"],  # ok | fail
    registry=REGISTRY,
)
frames_total = Counter(
    "gw_frames_total",
    "Frames processed, by type and verdict",
    ["type", "result"],  # result: ok | bad_hmac | malformed | replay | rate_limited | unauthorized
    registry=REGISTRY,
)
bytes_received_total = Counter(
    "gw_bytes_received_total", "Total application bytes read from clients", registry=REGISTRY,
)
events_forwarded_total = Counter(
    "gw_events_forwarded_total", "Valid events forwarded into the pipeline", ["kind"],  # tx | auth
    registry=REGISTRY,
)
malformed_total = Counter(
    "gw_malformed_total", "Malformed frames, by reason", ["reason"], registry=REGISTRY,
)
replays_rejected_total = Counter(
    "gw_replays_rejected_total", "Frames rejected as replays (seq or stale timestamp)",
    ["reason"],  # stale | seq_regression
    registry=REGISTRY,
)
rate_limited_total = Counter(
    "gw_rate_limited_total", "EVENT frames dropped by the per-connection rate limiter",
    registry=REGISTRY,
)
timeouts_total = Counter(
    "gw_timeouts_total", "Connections closed on timeout, by phase", ["phase"],  # handshake | idle
    registry=REGISTRY,
)
abuse_blocks_total = Counter(
    "gw_abuse_blocks_total", "Source IPs blocked for abuse (strikes exceeded)", registry=REGISTRY,
)
handshake_seconds = Histogram(
    "gw_handshake_seconds", "Time from connect to authenticated",
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5), registry=REGISTRY,
)
