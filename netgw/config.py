"""Configuration for the RTWP ingest gateway (env-driven, like the rest of RTACE)."""

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class GatewayConfig:
    host: str
    port: int

    # Authentication. In this demo every client authenticates with one shared
    # secret; a real deployment would key it per client_id from a secret store.
    shared_secret: bytes

    # Connection management / exhaustion defence.
    max_connections: int          # global cap on concurrent connections
    max_conns_per_ip: int         # per-source-IP cap

    # Timeouts (seconds). Defend against slowloris and idle hogging.
    handshake_timeout_s: float    # must finish HELLO/AUTH within this
    idle_timeout_s: float         # max silence between frames before disconnect

    # Rate limiting (token bucket) against floods / rate abuse.
    rate_capacity: int            # burst size per connection
    rate_refill_per_s: float      # sustained EVENT frames/sec per connection

    # Replay / freshness.
    replay_window_s: int          # reject frames whose timestamp is older than this

    # Abuse handling.
    max_strikes: int              # malformed/abusive frames before disconnect
    block_ttl_s: int              # how long an abusive IP is blocked (Redis)

    # TLS / mTLS (optional).
    tls_enabled: bool
    tls_certfile: str
    tls_keyfile: str
    tls_cafile: str               # if set, require & verify client certs (mTLS)

    metrics_port: int
    kafka_enabled: bool           # forward accepted events to Kafka
    redis_enabled: bool           # consult/refresh the shared IP blocklist

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        return cls(
            host=os.getenv("GATEWAY_HOST", "0.0.0.0"),
            port=int(os.getenv("GATEWAY_PORT", "9500")),
            shared_secret=os.getenv("RTWP_SECRET", "rtace-dev-secret").encode(),
            max_connections=int(os.getenv("GATEWAY_MAX_CONNECTIONS", "512")),
            max_conns_per_ip=int(os.getenv("GATEWAY_MAX_CONNS_PER_IP", "16")),
            handshake_timeout_s=float(os.getenv("GATEWAY_HANDSHAKE_TIMEOUT_S", "5")),
            idle_timeout_s=float(os.getenv("GATEWAY_IDLE_TIMEOUT_S", "30")),
            rate_capacity=int(os.getenv("GATEWAY_RATE_CAPACITY", "40")),
            rate_refill_per_s=float(os.getenv("GATEWAY_RATE_REFILL_PER_S", "20")),
            replay_window_s=int(os.getenv("GATEWAY_REPLAY_WINDOW_S", "30")),
            max_strikes=int(os.getenv("GATEWAY_MAX_STRIKES", "5")),
            block_ttl_s=int(os.getenv("GATEWAY_BLOCK_TTL_S", "300")),
            tls_enabled=os.getenv("GATEWAY_TLS_ENABLED", "false").lower() in ("1", "true", "yes"),
            tls_certfile=os.getenv("GATEWAY_TLS_CERTFILE", "deployment/certs/server.crt"),
            tls_keyfile=os.getenv("GATEWAY_TLS_KEYFILE", "deployment/certs/server.key"),
            tls_cafile=os.getenv("GATEWAY_TLS_CAFILE", ""),
            metrics_port=int(os.getenv("GATEWAY_METRICS_PORT", "9096")),
            kafka_enabled=os.getenv("GATEWAY_KAFKA_ENABLED", "true").lower() in ("1", "true", "yes"),
            redis_enabled=os.getenv("GATEWAY_REDIS_ENABLED", "true").lower() in ("1", "true", "yes"),
        )
