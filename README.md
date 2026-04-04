# RTACE — Real-Time Transaction Anomaly & Containment Engine

A real-time fraud detection pipeline that ingests transaction streams, detects threats (starting with **replay attacks**), and performs automated containment actions.

## Architecture

```
Transaction Simulator  →  Kafka (tx-events)   ─┐
                       →  Kafka (auth-events) ─┼→  Detection Engine
                                                      ↓
                                              Kafka (detections)
                                                      ↓
Containment Engine  ←  Redis (enforcement rules)  ←  Kafka (detections)
       ↓
Kafka (audit-log)
```

- **Redis**: state store (replay hashes, user sessions, burst windows, auth-fail windows, IP blocks, quarantine rules).
- **FastAPI**: control API for health and enforcement rules.
- **Prometheus**: scrapes metrics from the detection engine, containment engine, and API.
- **Grafana**: pre-provisioned with Prometheus as default data source and an RTACE starter dashboard.
- **Docker Compose**: runs Kafka, Zookeeper, Redis, Prometheus, and Grafana locally.

## Prerequisites

- Python 3.11+
- Docker and Docker Compose

## Quick Start

### 1. Start infrastructure (Kafka, Redis, Prometheus, Grafana)

From the **repository root** (parent of `rtace/`):

```bash
docker compose -f rtace/deployment/docker-compose.yml up -d
```

Wait until Kafka is healthy (e.g. 30–60 seconds). Topics `tx-events`, `auth-events`, `detections`, and `audit-log` are auto-created on first use.

### 2. Install Python dependencies

```bash
cd rtace
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Run the pipeline

Use **four terminals**, all from the `rtace` directory with the venv activated.

**Terminal 1 — Detection engine** (consumes tx-events, produces detections):

```bash
cd rtace && PYTHONPATH=. python -m detection_engine.consumer
```

**Terminal 2 — Containment engine** (consumes detections, writes Redis + audit-log):

```bash
cd rtace && PYTHONPATH=. python -m containment_engine.consumer
```

**Terminal 3 — Simulator** (produces **transaction** events to `tx-events` and **authentication** events to `auth-events`, with optional transaction replays and occasional failed-login bursts):

```bash
cd rtace && PYTHONPATH=. python -m simulator.transaction_simulator
```

**Terminal 4 — Control API** (optional):

```bash
cd rtace && PYTHONPATH=. uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

Order: start **detection** and **containment** first, then the **simulator**. The simulator will send transactions; some are replayed on purpose (`replay_probability=0.2`), so you should see replay detections and quarantine rules in logs and Redis.

## Prometheus metrics

Each component exposes Prometheus metrics:

| Component           | Metrics endpoint   | Port |
|---------------------|--------------------|------|
| Detection engine    | `http://localhost:9091/metrics` | 9091 |
| Containment engine  | `http://localhost:9093/metrics` | 9093 |
| Control API         | `http://localhost:8000/metrics` | 8000 |

**Metrics exposed:**

- `transactions_processed_total{outcome}` — transactions processed (outcome: `clean` \| `detected`; detected = any detector fired)
- `replay_detections_total{detection_type}` — replay attacks detected
- `geo_velocity_detections_total{detection_type}` — geo velocity (impossible travel) anomalies detected
- `fraud_burst_detections_total{detection_type}` — fraud burst (too many transactions in rolling window) detections
- `credential_stuffing_detections_total{scope}` — credential stuffing (`scope`: `user` \| `ip`)
- `containment_actions_total{detection_type,action}` — containment actions executed (`quarantine`, `ip_block` for credential stuffing)
- `redis_operation_latency_seconds{operation}` — Redis call latency (e.g. `replay_check`, `setex_quarantine`, `ping`, `scan_quarantine`)
- `detection_pipeline_latency_seconds` — time to process a transaction through the detection pipeline

**Viewing metrics locally**

1. Start the full stack (including Prometheus):

   ```bash
   docker compose -f rtace/deployment/docker-compose.yml up -d
   ```

2. Start the detection engine, containment engine, API, and simulator as in **Quick Start** above.

3. Open the Prometheus UI: **http://localhost:9090**

4. Example queries in Prometheus:
   - `rate(transactions_processed_total[1m])`
   - `replay_detections_total`
   - `rate(containment_actions_total[1m])`
   - `histogram_quantile(0.99, rate(detection_pipeline_latency_seconds_bucket[5m]))`
   - `redis_operation_latency_seconds_count`

5. To scrape metrics directly (without Prometheus):
   - `curl http://localhost:9091/metrics` (detection)
   - `curl http://localhost:9093/metrics` (containment)
   - `curl http://localhost:8000/metrics` (API)

## Grafana

Grafana is included in the Docker Compose stack and is provisioned at startup:

- **Prometheus** is configured as the default data source (no manual setup).
- A starter dashboard **RTACE — Fraud detection pipeline** is loaded from `deployment/grafana/provisioning/dashboards/rtace/rtace-dashboard.json`.

**Viewing dashboards locally**

1. Start the full stack (including Grafana):

   ```bash
   docker compose -f rtace/deployment/docker-compose.yml up -d
   ```

2. Open Grafana: **http://localhost:3000**

3. Log in with the default credentials:
   - Username: `admin`
   - Password: `admin`
   (Change the password when prompted, or set `GF_SECURITY_ADMIN_PASSWORD` in docker-compose to avoid the prompt.)

4. Go to **Dashboards** (left sidebar) → **RTACE** folder → **RTACE — Fraud detection pipeline**.

5. The dashboard includes panels for:
   - **Transactions processed (rate)** — `transactions_processed_total` by outcome (clean / detected)
   - **Replay detections (rate)** — `replay_detections_total`
   - **Containment actions (rate)** — `containment_actions_total`
   - **Detection pipeline latency** — p50/p99 of `detection_pipeline_latency_seconds`
   - **Redis operation latency** — p99 of `redis_operation_latency_seconds` by operation
   - **Geo velocity detections (rate)** — `geo_velocity_detections_total`
   - **Fraud burst detections (rate)** — `fraud_burst_detections_total`
   - **Credential stuffing detections (rate, by scope)** — `credential_stuffing_detections_total` (`user` vs `ip`)

6. Ensure the detection engine, containment engine, and (optionally) the simulator and API are running so Prometheus has data; then refresh or wait for the next scrape.

**Provisioning layout**

- Data source: `deployment/grafana/provisioning/datasources/datasources.yml`
- Dashboard provider and JSON: `deployment/grafana/provisioning/dashboards/` (provider in `dashboards.yml`, dashboards in `rtace/` subfolder)

## Example commands to test

- **Health and enforcement rules** (after containment has run):

  ```bash
  curl http://localhost:8000/health
  curl http://localhost:8000/enforcement/rules
  ```

- **Redis quarantine keys** (user quarantined for 1 hour after replay):

  ```bash
  redis-cli KEYS "enforce:quarantine:user:*"
  redis-cli TTL "enforce:quarantine:user:user_1"
  ```

- **Replay seen set** (per-day set of transaction hashes):

  ```bash
  redis-cli KEYS "replay:seen:*"
  redis-cli SMEMBERS "replay:seen:2025-03-08"
  ```

- **Fraud burst sorted sets** (per user, rolling 1m window in key name):

  ```bash
  redis-cli ZRANGE "burst:user_1:1m" 0 -1 WITHSCORES
  redis-cli TTL "burst:user_1:1m"
  ```

- **Credential stuffing** (failed-login rolling windows; IP keys use `-` instead of `:` for IPv6 safety):

  ```bash
  redis-cli ZRANGE "auth:fail:user:user_1:1m" 0 -1 WITHSCORES
  redis-cli ZRANGE "auth:fail:ip:198.51.100.250:1m" 0 -1 WITHSCORES
  ```

- **IP block** (after credential stuffing containment):

  ```bash
  redis-cli GET "block:ip:198.51.100.250"
  redis-cli TTL "block:ip:198.51.100.250"
  ```

## Configuration (environment)

| Variable | Default | Description |
|----------|---------|-------------|
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka brokers |
| `KAFKA_TX_EVENTS_TOPIC` | `tx-events` | Transaction events topic |
| `KAFKA_AUTH_EVENTS_TOPIC` | `auth-events` | Authentication / login events topic |
| `KAFKA_DETECTIONS_TOPIC` | `detections` | Detection events topic |
| `KAFKA_AUDIT_LOG_TOPIC` | `audit-log` | Audit log topic |
| `REDIS_HOST` | `localhost` | Redis host |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_DB` | `0` | Redis DB |
| `REDIS_REPLAY_TTL_HOURS` | `24` | TTL for replay seen set (hours) |
| `REDIS_QUARANTINE_TTL_SECONDS` | `3600` | Quarantine rule TTL (1 hour) |
| `REDIS_SESSION_TTL_DAYS` | `7` | User session (last location) TTL for geo velocity (days) |
| `REDIS_BURST_WINDOW_SECONDS` | `60` | Rolling window length for fraud burst (seconds) |
| `REDIS_BURST_THRESHOLD` | `20` | Max transactions allowed in the window; detection when count **exceeds** this (i.e. 21+ in 60s by default) |
| `REDIS_BURST_KEY_TTL_SECONDS` | `120` | TTL on `burst:{user_id}:1m` sorted set keys (auto-expire when idle) |
| `REDIS_AUTH_FAIL_WINDOW_SECONDS` | `60` | Rolling window for failed-login tracking (credential stuffing) |
| `REDIS_AUTH_FAIL_USER_THRESHOLD` | `10` | Per-user failed logins in window before detection; fires when count **exceeds** (11+) |
| `REDIS_AUTH_FAIL_IP_THRESHOLD` | `50` | Per-IP failed logins in window before detection; fires when count **exceeds** (51+) |
| `REDIS_AUTH_FAIL_KEY_TTL_SECONDS` | `120` | TTL on `auth:fail:*` sorted set keys |
| `REDIS_IP_BLOCK_TTL_SECONDS` | `3600` | TTL on `block:ip:{ip}` after credential stuffing containment (1 hour) |

## Project structure

```
rtace/
├── simulator/           # Transaction + auth event generator (tx-events, auth-events)
├── detection_engine/    # Detectors; consumes tx-events + auth-events → detections
├── containment_engine/ # Detections → Redis rules + audit-log
├── api/                 # FastAPI control API
├── common/              # Models, Kafka/Redis clients
├── configs/             # Kafka and Redis config
├── deployment/          # docker-compose.yml, prometheus.yml, grafana/provisioning
└── README.md
```

## Event types

- **Transaction events** (`tx-events`): same schema as before (`user_id`, `amount`, `merchant`, `location`, `latitude` / `longitude`, etc.).
- **Authentication events** (`auth-events`): `event_id`, `user_id`, `ip_address`, `success` (boolean), `timestamp`. Used for credential stuffing detection only (failed attempts).

## Detection modules

**Replay detection**

- Each transaction is hashed (user, amount, merchant, timestamp, location).
- Hashes are stored in Redis set `replay:seen:{date}` with 24h TTL.
- If the same hash is seen again within the window → **replay_attack** detection is emitted to `detections`.
- Containment: key `enforce:quarantine:user:{user_id}` is set in Redis with TTL 1 hour.

**Geo velocity (impossible travel)**

- For each transaction, the user’s last location and timestamp are read from Redis key `session:{user_id}` (hash: `last_latitude`, `last_longitude`, `last_timestamp`).
- Distance is computed with the Haversine formula; velocity = distance_km / time_hours.
- If velocity > 900 km/h → **geo_velocity_anomaly** detection (severity high) is emitted.
- Session is updated with the current transaction’s coordinates and timestamp; key TTL is 7 days.
- Containment: same quarantine as replay (`enforce:quarantine:user:{user_id}`).

**Fraud burst (rolling transaction rate)**

- Per user, a Redis **sorted set** `burst:{user_id}:1m` stores recent transactions: **member** = transaction id (`event_id`), **score** = unix timestamp (seconds).
- On each transaction: remove members with score **older than** the rolling window (default **60 seconds**), add the current transaction, **count** members in the set.
- If the count **exceeds** the configurable threshold (default **20**, i.e. **21+** transactions in the window), emit **fraud_burst** (severity high) to `detections`.
- The sorted set key is given a **TTL** (default **120 seconds**) so it expires automatically after inactivity.
- Metric: `fraud_burst_detections_total{detection_type}`.
- Grafana: **Fraud burst detections (rate)** panel on the RTACE dashboard.
- Containment: same quarantine as other high-severity detections (`enforce:quarantine:user:{user_id}`).

**Credential stuffing**

- Consumes **authentication events** from `auth-events` (failed logins only; successes are ignored).
- **Account-targeted:** Redis sorted set `auth:fail:user:{user_id}:1m` — member = `event_id`, score = unix time. Entries older than the rolling window (default **60s**) are removed. If count **exceeds** the user threshold (default **10**, i.e. **11+** fails), emit **credential_stuffing** with metric scope `user`.
- **IP-targeted:** Redis sorted set `auth:fail:ip:{ip}:1m` (IPv6 addresses use `-` instead of `:` in the key). Same trim/append pattern. If count **exceeds** the IP threshold (default **50**, i.e. **51+**), emit **credential_stuffing** with scope `ip`.
- Both strategies can fire on the same failed login; separate detection events are emitted (`det-cred-user-…` vs `det-cred-ip-…`).
- Detection payload includes `user_id`, `ip_address`, `transaction_id` (auth `event_id`), `severity` high.
- Metrics: `credential_stuffing_detections_total{scope="user|ip"}`.
- Grafana: **Credential stuffing detections (rate, by scope)**.
- Containment: user quarantine (`enforce:quarantine:user:{user_id}`) **and** IP block (`block:ip:{ip}`, TTL **1 hour** by default). Audit log action `quarantine+ip_block` when IP is present.

## License

Internal use. Adjust as needed for your organization.
