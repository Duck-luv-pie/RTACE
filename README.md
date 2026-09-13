# RTACE — Real-Time Transaction Anomaly & Containment Engine

A real-time fraud detection pipeline that ingests transaction and login streams, detects replay attacks, impossible travel, transaction bursts and credential stuffing, applies tiered containment, and **enforces** the resulting rules on subsequent traffic.

[![CI](https://github.com/Duck-luv-pie/RTACE/actions/workflows/ci.yml/badge.svg)](https://github.com/Duck-luv-pie/RTACE/actions/workflows/ci.yml)

## Architecture

```
Simulator ─► Kafka tx-events   ──┐
          ─► Kafka auth-events ──┼─► Detection Engine ──► Kafka detections ──► Containment Engine
                                 │     │        ▲                                   │
                                 │     │        │ reads rules                       │ writes rules
                                 │     ▼        │                                   ▼
                                 │   blocks ◄── Redis (enforcement: quarantine / step-up / ip block)
                                 │                Redis (state: replay keys, sessions, windows, cooldowns)
                                 │
                                 └─► poison records ──► Kafka rtace-dlq
   Detection + Containment + API ──► Kafka audit-log        Control API ◄──► Redis (list / override rules)
```

- **Redis**: state store (replay keys, user sessions, burst and auth-fail windows, cooldowns) **and** enforcement store (quarantines, step-ups, IP blocks). Runs with `noeviction` + AOF so a rule is never silently dropped.
- **FastAPI**: control API for health, enforcement rules and manual overrides (token-protected).
- **Prometheus**: scrapes metrics from the detection engine, containment engine, and API.
- **Grafana**: pre-provisioned with Prometheus as default data source and an RTACE starter dashboard.
- **Docker Compose**: runs Kafka (KRaft, no ZooKeeper), Redis, Prometheus and Grafana locally, with persistent volumes; `--profile app` also builds and runs the four Python services.

## Prerequisites

- Docker and Docker Compose
- Python 3.11, 3.12 or 3.13 (only if you run the services on the host; CI tests all three)

## Quick Start

### 1. Start infrastructure (Kafka, Redis, Prometheus, Grafana)

From the repository root:

```bash
docker compose -f deployment/docker-compose.yml up -d
```

Wait until Kafka is healthy (20–40 seconds; `docker compose -f deployment/docker-compose.yml ps` shows `healthy`). The `kafka-init` service creates the `tx-events`, `auth-events`, `detections`, and `audit-log` topics automatically (16 partitions each, so multiple consumer processes can run in parallel), plus the `rtace-dlq` dead-letter topic.

**Alternative: run everything in containers.** Skip steps 2 and 3 entirely:

```bash
docker compose -f deployment/docker-compose.yml --profile app up -d --build
docker compose -f deployment/docker-compose.yml logs -f detection-engine containment-engine simulator
```

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

**Terminal 3 — Simulator** (produces **transaction** events to `tx-events` and **authentication** events to `auth-events`, with tagged attack scenarios; see **Simulator**):

```bash
PYTHONPATH=. python -m simulator.transaction_simulator            # defaults
PYTHONPATH=. python -m simulator.transaction_simulator --help     # all knobs
```

**Terminal 4 — Control API** (optional):

```bash
PYTHONPATH=. uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

Order: start **detection** and **containment** first, then the **simulator**. Within a minute or two you should see `scenario=` lines in the simulator, matching detections in the detection engine, `Containment applied` lines in the containment engine, and `Blocked transaction` lines once quarantined users keep sending.

### 4. Run the tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite runs against an in-memory Redis (`fakeredis`) and fake Kafka consumers, so it needs no infrastructure. It covers every detector, the cooldown, the session cache, the consumer loop (commit / retry / DLQ / shutdown), containment policy, enforcement, the API and the simulator.

### Running without Docker (Homebrew on macOS)

```bash
brew install redis kafka                     # Kafka 4.x runs in KRaft mode out of the box
# Match the compose settings: never evict enforcement keys; persist them
sed -i '' -e 's/^# *maxmemory-policy .*/maxmemory-policy noeviction/' -e 's/^appendonly no$/appendonly yes/' "$(brew --prefix)/etc/redis.conf"
echo 'auto.create.topics.enable=false' >> "$(brew --prefix)/etc/kafka/server.properties"
brew services start redis && brew services start kafka
for t in tx-events:16 auth-events:16 detections:16 audit-log:16 rtace-dlq:4; do
  kafka-topics --bootstrap-server localhost:9092 --create --if-not-exists --topic "${t%%:*}" --partitions "${t##*:}" --replication-factor 1
done
```

Then run the Python services exactly as above. Notes:

- A local Kafka's KRaft controller listens on **9093**, which is why the containment engine's metrics port is 9094.
- kafka-python first tries `localhost` over IPv6, logs one `InvalidReceiveError` at startup and reconnects over IPv4. It is harmless; set `KAFKA_BOOTSTRAP_SERVERS=127.0.0.1:9092` to silence it.
- Prometheus and Grafana are not part of this path; scrape the `/metrics` endpoints directly or `brew install prometheus grafana` and point them at the same ports.

## Performance

Measured on an Apple Silicon laptop with Kafka and Redis installed via Homebrew, one detection-engine process, no contention (2026-09):

| Path | Redis round trips per record | Throughput per process | p50 / p99 pipeline latency |
|------|------------------------------|------------------------|----------------------------|
| Transaction, full detector path | 4 (enforcement, replay, session write, burst) | ~4,400 records/s | 0.5 ms / 1.0 ms |
| Successful login or blocked event | 1 | ~19,000 records/s | |
| Mixed backlog (50/50) | | ~7,000 records/s | |

The producer side is not the bottleneck (the simulator sustained ~35,000 events/s). The engine is bound by sequential Redis round trips plus per-record Python work, so it scales linearly with processes up to the 16 partitions per topic, and a single Redis instance saturates around 25,000 full-path transactions/s. To go beyond that: pipeline Redis work across a whole poll batch, collapse the per-transaction checks into one Lua script, switch to confluent-kafka, and shard Redis.

## Prometheus metrics

Each component exposes Prometheus metrics:

| Component           | Metrics endpoint   | Port |
|---------------------|--------------------|------|
| Detection engine    | `http://localhost:9091/metrics` | 9091 |
| Containment engine  | `http://localhost:9094/metrics` | 9094 |
| Control API         | `http://localhost:8000/metrics` | 8000 |

**Metrics exposed:**

- `transactions_processed_total{outcome}` — transactions processed (outcome: `clean` \| `detected` \| `blocked`; detected = any detector fired)
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
- `containment_actions_total{detection_type,action}` — containment actions executed (`quarantine` \| `step_up_auth` \| `ip_block`)
- `events_blocked_total{event_type,reason}` — events the detection engine refused because a rule was active (`transaction`/`quarantine`, `auth`/`ip_block`); blocked transactions also appear as `transactions_processed_total{outcome="blocked"}`
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
   - `curl http://localhost:9094/metrics` (containment)
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
   - **Events blocked by enforcement**, **Detections suppressed by cooldown**, **Consumer processing errors and DLQ**, **Kafka send failures**

6. Ensure the detection engine, containment engine, and (optionally) the simulator and API are running so Prometheus has data; then refresh or wait for the next scrape.

**Provisioning layout**

- Data source: `deployment/grafana/provisioning/datasources/datasources.yml`
- Dashboard provider and JSON: `deployment/grafana/provisioning/dashboards/` (provider in `dashboards.yml`, dashboards in `rtace/` subfolder)

## Example commands to test

- **Health and enforcement rules** (after containment has run):

  ```bash
  curl http://localhost:8000/health
  curl -H "Authorization: Bearer $RTACE_API_TOKEN" http://localhost:8000/enforcement/rules
  ```

- **Manual overrides** (require `RTACE_API_TOKEN`; each one is written to `audit-log` with `source: api`):

  ```bash
  H="Authorization: Bearer $RTACE_API_TOKEN"
  curl -X POST   -H "$H" "http://localhost:8000/enforcement/quarantine/user_3?ttl_seconds=600&reason=fraud-desk"
  curl -X DELETE -H "$H"  http://localhost:8000/enforcement/quarantine/user_3
  curl -X DELETE -H "$H"  http://localhost:8000/enforcement/step-up/user_2      # user completed step-up auth
  curl -X POST   -H "$H"  http://localhost:8000/enforcement/ip-block/203.0.113.9
  curl -X DELETE -H "$H"  http://localhost:8000/enforcement/ip-block/203.0.113.9
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
| `DETECTION_METRICS_PORT` | `9091` | Prometheus port of the detection engine |
| `CONTAINMENT_METRICS_PORT` | `9094` | Prometheus port of the containment engine (not 9093, the KRaft controller port) |
| `REDIS_HOST` | `localhost` | Redis host |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_DB` | `0` | Redis DB |
| `REDIS_REPLAY_TTL_HOURS` | `24` | Sliding TTL of each `replay:seen:{hash}` key (hours) |
| `REDIS_QUARANTINE_TTL_SECONDS` | `3600` | Quarantine rule TTL (1 hour) |
| `REDIS_STEP_UP_TTL_SECONDS` | `900` | How long a geo-velocity step-up requirement stays pending (15 min) |
| `RTACE_API_TOKEN` | unset | Bearer token for `/enforcement`. When unset, reads are open and manual overrides answer 503 |
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
├── simulator/           # Traffic generator with tagged attack scenarios (tx-events, auth-events)
├── detection_engine/    # Detectors, cooldown, DetectionPipeline; tx-events + auth-events → detections
├── containment_engine/  # ContainmentPipeline + policy; detections → Redis rules + audit-log
├── api/                 # FastAPI control API (rules, manual overrides, health, metrics)
├── common/              # Models, Kafka/Redis clients, consumer loop, enforcement reads, metrics
├── configs/             # Kafka and Redis/detector config (all env-driven)
├── tests/               # pytest suite (fakeredis + fake consumers; no infrastructure needed)
├── deployment/          # docker-compose.yml (KRaft Kafka, Redis, Prometheus, Grafana, app profile)
├── Dockerfile           # One image for all four Python services
├── .github/workflows/   # CI: import check + tests on Python 3.11 / 3.12 / 3.13
└── README.md
```

## Enforcement and containment policy

Enforcement rules are Redis keys with TTLs, and the detection engine **checks them before analysing an event**:

| Rule | Key | Effect in the detection engine |
|------|-----|--------------------------------|
| Quarantine | `enforce:quarantine:user:{user_id}` | Transactions from the user are refused: no detectors run, the trusted location and burst window do not advance, `events_blocked_total{transaction,quarantine}` is incremented and a `transaction_blocked` record goes to `audit-log` |
| IP block | `block:ip:{ip}` | Login attempts from the IP are refused and do not feed the stuffing counters (`events_blocked_total{auth,ip_block}`, `auth_blocked` audit record) |
| Step-up | `enforce:step_up:user:{user_id}` | Nothing is blocked. The key signals to the authentication layer that the user must re-authenticate; clear it with `DELETE /enforcement/step-up/{user_id}` once they have |

Each key's value is the detection id (or `api:<reason>` for manual overrides), so `GET /enforcement/rules` shows who set every rule and how long it has left.

**Containment policy** (containment engine, per detection type):

| Detection | Signal quality | Action |
|-----------|----------------|--------|
| `replay_attack` | hard: an exact request was resent | quarantine user |
| `fraud_burst` | hard: rate far above normal | quarantine user |
| `credential_stuffing` (IP scope) | hard: one IP hammering many accounts | quarantine user **and** block IP |
| `credential_stuffing` (user scope) | hard for the account, but the IP on the detection is only whichever login tipped the count, possibly the real user | quarantine user only |
| `geo_velocity_anomaly` | soft: VPNs, shared accounts and coarse geolocation trip it | **step-up auth** for `REDIS_STEP_UP_TTL_SECONDS`; if another anomaly arrives while a step-up is already pending, **escalate to quarantine** |

Every action is idempotent (SETEX), so redelivered detections are harmless.

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

## Simulator

Each simulated user has a **home city**; nearly all of their transactions come from there, so geo velocity sees realistic traffic. Attacks are injected on top and logged with a `scenario=` tag:

| Scenario | What is sent | Default schedule | Detector it exercises |
|----------|--------------|------------------|-----------------------|
| `replay` | a user's previous transaction, byte-for-byte | 5% of iterations | replay |
| `impossible_travel` | one transaction from a city far from home | 2% of iterations | geo velocity |
| `fraud_burst` | 25 transactions for one user in a few seconds | every 200 iterations | fraud burst (threshold 20) |
| `stuffing_user` | 12 failed logins against one account | every 150 iterations | credential stuffing (user) |
| `stuffing_ip` | 52 failed logins from one IP across accounts | every 400 iterations | credential stuffing (IP) |

Quarantined users keep sending, which is what an attacker does, so you also see enforcement working (`Blocked transaction` in the detection engine, `events_blocked_total` in Grafana).

Knobs, as environment variables or flags (flags win): `SIM_INTERVAL_SECONDS` / `--interval` (default 0.5), `SIM_USERS` / `--users` (20), `SIM_REPLAY_PROBABILITY` / `--replay-p`, `SIM_IMPOSSIBLE_TRAVEL_PROBABILITY` / `--travel-p`, `SIM_AUTH_FAIL_PROBABILITY` / `--auth-fail-p`, `SIM_BURST_EVERY` / `--burst-every`, `SIM_STUFFING_USER_EVERY`, `SIM_STUFFING_IP_EVERY` (0 disables a scenario), `SIM_SEED` / `--seed` for a reproducible stream.

## Event types

- **Transaction events** (`tx-events`): `event_id`, `user_id`, `amount`, `merchant`, `timestamp`, `location`, `latitude`, `longitude`.
- **Authentication events** (`auth-events`): `event_id`, `user_id`, `ip_address`, `success` (boolean), `timestamp`. Used for credential stuffing detection only (failed attempts).

## Detection modules

**Replay detection**

- Each transaction is hashed (user, amount to the cent, merchant, timestamp, location). The timestamp is part of the hash on purpose: a replay resends a captured request byte-for-byte, and excluding it would make two identical legitimate purchases on the same day a false positive.
- Each hash gets its own key `replay:seen:{hash}` written with `SET NX EX`, so the 24h window slides per transaction. There is no day-boundary hole and unrelated writes never refresh another transaction's expiry.
- The key's value is the Kafka record (`topic:partition:offset`) that first carried the transaction. Kafka is at-least-once: if the **same record** is delivered again after a crash, the stored value matches and it is counted in `replay_redeliveries_total`, not flagged. A real replay arrives in a **different** record → **replay_attack** (severity high) is emitted with the original record in `details.first_seen_record`.
- Containment: quarantine (see **Containment policy**).

**Geo velocity (impossible travel)**

- For each transaction, the user’s last location and timestamp are read from Redis key `session:{user_id}` (hash: `last_latitude`, `last_longitude`, `last_timestamp`).
- Distance is computed with the Haversine formula; velocity = distance_km / time_hours (gaps under one second are scored as one second, so a large jump in a tiny interval is flagged rather than skipped).
- If velocity > `GEO_MAX_VELOCITY_KMH` (default 900) → **geo_velocity_anomaly** detection (severity high) with distance, elapsed time and velocity in `details`.
- The session is the user's last **trusted** position. It advances only on clean transactions: a flagged location never becomes the baseline (otherwise the user's next purchase from home would be flagged too), and a transaction older than the baseline (late/out-of-order delivery) is ignored rather than rewinding it. A genuine relocation is accepted once enough time has passed for the implied speed to be plausible.
- Session hash `session:{user_id}` has a 7-day TTL and is fronted by a per-process TTL cache (`SESSION_CACHE_*`) that is cleared whenever the consumer loses Kafka partitions.
- Containment: **step-up authentication**, escalating to quarantine on a repeat (see **Containment policy**).

**Fraud burst (rolling transaction rate)**

- Per user, a Redis **sorted set** `burst:{user_id}:1m` stores recent transactions: **member** = transaction id (`event_id`), **score** = unix timestamp (seconds).
- On each transaction: remove members with score **older than** the rolling window (default **60 seconds**), add the current transaction, **count** members in the set.
- If the count **exceeds** the configurable threshold (default **20**, i.e. **21+** transactions in the window), emit **fraud_burst** (severity high) to `detections`.
- The sorted set key is given a **TTL** (default **120 seconds**) so it expires automatically after inactivity.
- Metric: `fraud_burst_detections_total{detection_type}`.
- Grafana: **Fraud burst detections (rate)** panel on the RTACE dashboard.
- Containment: quarantine (see **Containment policy**).

**Credential stuffing**

- Consumes **authentication events** from `auth-events` (failed logins only; successes are ignored).
- **Account-targeted:** Redis sorted set `auth:fail:user:{user_id}:1m` — member = `event_id`, score = unix time. Entries older than the rolling window (default **60s**) are removed. If count **exceeds** the user threshold (default **10**, i.e. **11+** fails), emit **credential_stuffing** with metric scope `user`.
- **IP-targeted:** Redis sorted set `auth:fail:ip:{ip}:1m` (IPv6 addresses use `-` instead of `:` in the key). Same trim/append pattern. If count **exceeds** the IP threshold (default **50**, i.e. **51+**), emit **credential_stuffing** with scope `ip`.
- Both strategies can fire on the same failed login; separate detection events are emitted (`det-cred-user-…` vs `det-cred-ip-…`).
- Detection payload includes `user_id`, `ip_address`, `transaction_id` (auth `event_id`), `severity` high.
- Metrics: `credential_stuffing_detections_total{scope="user|ip"}`.
- Grafana: **Credential stuffing detections (rate, by scope)**.
- Containment: user quarantine (`enforce:quarantine:user:{user_id}`) for both scopes; IP block (`block:ip:{ip}`, TTL **1 hour** by default) for the **IP scope only**. Audit log action is `quarantine+ip_block` or `quarantine`.

**Detection cooldown**

- Window-based detectors stay above threshold for as long as an attack continues, so without suppression every further event would produce another detection, containment write and audit record.
- The first detection for a given `(detection_type, subject)` is emitted; repeats within `DETECTION_COOLDOWN_SECONDS` (default 60) are dropped and counted in `detections_suppressed_total`. The subject is the user, or the IP for IP-scope credential stuffing. Implemented with `SET cooldown:{type}:{subject} NX EX`, so it also holds across several detection-engine processes.
- Containment is idempotent, so a suppressed repeat loses nothing: the subject is already quarantined or blocked.

**Detection identity**

- `detection_id` is a fresh UUID per detection (`det-replay-…`, `det-geo-…`, `det-burst-…`, `det-cred-user-…`, `det-cred-ip-…`). It is never derived from the triggering event id, because one event can legitimately yield several detections over time. `transaction_id` carries the triggering event id.
- Every detection has a `details` object with the detector's evidence (counts, window, velocity, scope, first-seen record).

## License

MIT — see [LICENSE](LICENSE).
