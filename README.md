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

From the repository root:

```bash
docker compose -f deployment/docker-compose.yml up -d
```

Wait until Kafka is healthy (e.g. 30–60 seconds). The `kafka-init` service creates the `tx-events`, `auth-events`, `detections`, and `audit-log` topics automatically (16 partitions each, so multiple consumer processes can run in parallel), plus the `rtace-dlq` dead-letter topic.

### 2. Install Python dependencies

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Run the pipeline

Use **four terminals**, all from the repository root with the venv activated.

**Terminal 1 — Detection engine** (consumes tx-events, produces detections):

```bash
PYTHONPATH=. python -m detection_engine.consumer
```

**Terminal 2 — Containment engine** (consumes detections, writes Redis + audit-log):

```bash
PYTHONPATH=. python -m containment_engine.consumer
```

**Terminal 3 — Simulator** (produces **transaction** events to `tx-events` and **authentication** events to `auth-events`, with optional transaction replays and occasional failed-login bursts):

```bash
PYTHONPATH=. python -m simulator.transaction_simulator
```

**Terminal 4 — Control API** (optional):

```bash
PYTHONPATH=. uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
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
- `detections_suppressed_total{detection_type}` — detections dropped by the per-subject cooldown (see **Detection cooldown**)
- `replay_redeliveries_total` — transactions whose replay key was written by the *same* Kafka record (at-least-once redelivery, not a replay)
- `session_cache_requests_total{result}` — L1 session cache lookups (`hit` \| `miss`)
- `consumer_records_total{stage,result}` — records handled by each consumer loop (`processed` \| `dlq` \| `skipped`)
- `processing_errors_total{stage,kind}` — handler failures (`transient` = retried in place, `permanent` = dead-lettered)
- `dlq_messages_total{stage}` — records written to `rtace-dlq`
- `kafka_send_failures_total{topic}` — async producer sends that failed after retries
- `containment_actions_total{detection_type,action}` — containment actions executed (`quarantine`, `ip_block` for credential stuffing)
- `redis_operation_latency_seconds{operation}` — Redis call latency (e.g. `replay_check`, `setex_quarantine`, `ping`, `scan_quarantine`)
- `detection_pipeline_latency_seconds` — time to process a transaction through the detection pipeline

**Viewing metrics locally**

1. Start the full stack (including Prometheus):

   ```bash
   docker compose -f deployment/docker-compose.yml up -d
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
   docker compose -f deployment/docker-compose.yml up -d
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

- **Replay keys** (one key per transaction hash, value = Kafka record that first carried it):

  ```bash
  redis-cli --scan --pattern "replay:seen:*" | head
  redis-cli GET "replay:seen:<hash>"      # e.g. tx-events:3:1287
  redis-cli TTL "replay:seen:<hash>"
  ```

- **Cooldown keys** (one per detection type + subject while suppression is active):

  ```bash
  redis-cli --scan --pattern "cooldown:*"
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
| `KAFKA_DLQ_TOPIC` | `rtace-dlq` | Dead-letter topic for records that cannot be processed |
| `KAFKA_FETCH_MIN_BYTES` | `1` | Return a fetch as soon as any data is available (lowest latency). Raise with `KAFKA_FETCH_MAX_WAIT_MS` to batch harder under load |
| `KAFKA_FETCH_MAX_WAIT_MS` | `100` | Max broker wait when `KAFKA_FETCH_MIN_BYTES` is not yet satisfied |
| `KAFKA_MAX_POLL_RECORDS` | `500` | Records per poll |
| `KAFKA_POLL_TIMEOUT_MS` | `1000` | Poll timeout; also the shutdown-check interval |
| `REDIS_HOST` | `localhost` | Redis host |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_DB` | `0` | Redis DB |
| `REDIS_REPLAY_TTL_HOURS` | `24` | Sliding TTL of each `replay:seen:{hash}` key (hours) |
| `REDIS_QUARANTINE_TTL_SECONDS` | `3600` | Quarantine rule TTL (1 hour) |
| `REDIS_SESSION_TTL_DAYS` | `7` | User session (last location) TTL for geo velocity (days) |
| `SESSION_CACHE_MAXSIZE` | `10000` | Entries in the in-process (L1) session cache |
| `SESSION_CACHE_TTL_SECONDS` | `300` | Max age of an L1 session entry before Redis is consulted again |
| `GEO_MAX_VELOCITY_KMH` | `900` | Implied speed above which a transaction is an impossible-travel anomaly |
| `REDIS_BURST_WINDOW_SECONDS` | `60` | Rolling window length for fraud burst (seconds) |
| `REDIS_BURST_THRESHOLD` | `20` | Max transactions allowed in the window; detection when count **exceeds** this (i.e. 21+ in 60s by default) |
| `REDIS_BURST_KEY_TTL_SECONDS` | `120` | TTL on `burst:{user_id}:1m` sorted set keys (auto-expire when idle) |
| `REDIS_AUTH_FAIL_WINDOW_SECONDS` | `60` | Rolling window for failed-login tracking (credential stuffing) |
| `REDIS_AUTH_FAIL_USER_THRESHOLD` | `10` | Per-user failed logins in window before detection; fires when count **exceeds** (11+) |
| `REDIS_AUTH_FAIL_IP_THRESHOLD` | `50` | Per-IP failed logins in window before detection; fires when count **exceeds** (51+) |
| `REDIS_AUTH_FAIL_KEY_TTL_SECONDS` | `120` | TTL on `auth:fail:*` sorted set keys |
| `REDIS_IP_BLOCK_TTL_SECONDS` | `3600` | TTL on `block:ip:{ip}` after credential stuffing containment (1 hour) |
| `DETECTION_COOLDOWN_SECONDS` | `60` | Suppress repeat detections of the same type for the same user/IP within this window; `0` disables |

## Project structure

```
RTACE/
├── simulator/           # Transaction + auth event generator (tx-events, auth-events)
├── detection_engine/    # Detectors + pipeline; consumes tx-events + auth-events → detections
├── tests/               # pytest suite (fakeredis-backed, no Kafka needed)
├── containment_engine/ # Detections → Redis rules + audit-log
├── api/                 # FastAPI control API
├── common/              # Models, Kafka/Redis clients, shared consumer loop, metrics
├── configs/             # Kafka and Redis config
├── deployment/          # docker-compose.yml, prometheus.yml, grafana/provisioning
└── README.md
```

## Delivery guarantees and failure handling

Both consumers share one loop (`common/consumer_loop.py`) with these rules:

- **At-least-once, commit after processing.** Auto-commit is off. Offsets are committed synchronously after each poll batch, and only for records that were processed or deliberately dead-lettered. Every detector and containment action tolerates redelivery (replay detection recognises the same Kafka record; sorted-set members are event ids; enforcement writes are SETEX).
- **Transient failures block, they do not skip.** If Redis is unreachable, timing out, loading, out of memory or read-only, the failing record is retried in place with exponential backoff (0.5s → 30s) and its offset is not committed. Detections are not lost while a dependency is down; the pipeline visibly stalls and `processing_errors_total{kind="transient"}` climbs.
- **Poison records go to the DLQ.** Undecodable JSON, schema validation failures and unexpected exceptions inside a handler are written to `rtace-dlq` with the source topic/partition/offset, the base64 raw payload and the error, then committed past so one bad record cannot wedge a partition. Inspect with:

  ```bash
  kafka-console-consumer --bootstrap-server localhost:9092 --topic rtace-dlq --from-beginning
  ```

- **Graceful shutdown.** SIGINT/SIGTERM finish the current batch (or abandon a record mid-retry without committing it), commit, flush the producer and close both clients.
- **Producer durability.** Producers use `acks=all` with the idempotent producer, so a retried batch cannot duplicate or reorder records within a partition, and send failures are counted in `kafka_send_failures_total`.

## Event types

- **Transaction events** (`tx-events`): same schema as before (`user_id`, `amount`, `merchant`, `location`, `latitude` / `longitude`, etc.).
- **Authentication events** (`auth-events`): `event_id`, `user_id`, `ip_address`, `success` (boolean), `timestamp`. Used for credential stuffing detection only (failed attempts).

## Detection modules

**Replay detection**

- Each transaction is hashed (user, amount to the cent, merchant, timestamp, location). The timestamp is part of the hash on purpose: a replay resends a captured request byte-for-byte, and excluding it would make two identical legitimate purchases on the same day a false positive.
- Each hash gets its own key `replay:seen:{hash}` written with `SET NX EX`, so the 24h window slides per transaction. There is no day-boundary hole and unrelated writes never refresh another transaction's expiry.
- The key's value is the Kafka record (`topic:partition:offset`) that first carried the transaction. Kafka is at-least-once: if the **same record** is delivered again after a crash, the stored value matches and it is counted in `replay_redeliveries_total`, not flagged. A real replay arrives in a **different** record → **replay_attack** (severity high) is emitted with the original record in `details.first_seen_record`.
- Containment: key `enforce:quarantine:user:{user_id}` is set in Redis with TTL 1 hour.

**Geo velocity (impossible travel)**

- For each transaction, the user’s last location and timestamp are read from Redis key `session:{user_id}` (hash: `last_latitude`, `last_longitude`, `last_timestamp`).
- Distance is computed with the Haversine formula; velocity = distance_km / time_hours (gaps under one second are scored as one second, so a large jump in a tiny interval is flagged rather than skipped).
- If velocity > `GEO_MAX_VELOCITY_KMH` (default 900) → **geo_velocity_anomaly** detection (severity high) with distance, elapsed time and velocity in `details`.
- The session is the user's last **trusted** position. It advances only on clean transactions: a flagged location never becomes the baseline (otherwise the user's next purchase from home would be flagged too), and a transaction older than the baseline (late/out-of-order delivery) is ignored rather than rewinding it. A genuine relocation is accepted once enough time has passed for the implied speed to be plausible.
- Session hash `session:{user_id}` has a 7-day TTL and is fronted by a per-process TTL cache (`SESSION_CACHE_*`) that is cleared whenever the consumer loses Kafka partitions.
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

**Detection cooldown**

- Window-based detectors stay above threshold for as long as an attack continues, so without suppression every further event would produce another detection, containment write and audit record.
- The first detection for a given `(detection_type, subject)` is emitted; repeats within `DETECTION_COOLDOWN_SECONDS` (default 60) are dropped and counted in `detections_suppressed_total`. The subject is the user, or the IP for IP-scope credential stuffing. Implemented with `SET cooldown:{type}:{subject} NX EX`, so it also holds across several detection-engine processes.
- Containment is idempotent, so a suppressed repeat loses nothing: the subject is already quarantined or blocked.

**Detection identity**

- `detection_id` is a fresh UUID per detection (`det-replay-…`, `det-geo-…`, `det-burst-…`, `det-cred-user-…`, `det-cred-ip-…`). It is never derived from the triggering event id, because one event can legitimately yield several detections over time. `transaction_id` carries the triggering event id.
- Every detection has a `details` object with the detector's evidence (counts, window, velocity, scope, first-seen record).

## License

MIT — see [LICENSE](LICENSE).
