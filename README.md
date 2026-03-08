# RTACE — Real-Time Transaction Anomaly & Containment Engine

A real-time fraud detection pipeline that ingests transaction streams, detects threats (starting with **replay attacks**), and performs automated containment actions.

## Architecture

```
Transaction Simulator  →  Kafka (tx-events)  →  Detection Engine
                                                      ↓
                                              Kafka (detections)
                                                      ↓
Containment Engine  ←  Redis (enforcement rules)  ←  Kafka (detections)
       ↓
Kafka (audit-log)
```

- **Redis**: state store (replay hashes, quarantine rules).
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

Wait until Kafka is healthy (e.g. 30–60 seconds). Topics `tx-events`, `detections`, and `audit-log` are auto-created on first use.

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

**Terminal 3 — Transaction simulator** (produces tx-events, with optional replays):

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

- `transactions_processed_total{status}` — transactions processed (status: `ok` \| `replay`)
- `replay_detections_total{detection_type}` — replay attacks detected
- `containment_actions_total{detection_type,action}` — containment actions executed
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
   - **Transactions processed (rate)** — `transactions_processed_total` by status
   - **Replay detections (rate)** — `replay_detections_total`
   - **Containment actions (rate)** — `containment_actions_total`
   - **Detection pipeline latency** — p50/p99 of `detection_pipeline_latency_seconds`
   - **Redis operation latency** — p99 of `redis_operation_latency_seconds` by operation

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

## Configuration (environment)

| Variable | Default | Description |
|----------|---------|-------------|
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka brokers |
| `KAFKA_TX_EVENTS_TOPIC` | `tx-events` | Transaction events topic |
| `KAFKA_DETECTIONS_TOPIC` | `detections` | Detection events topic |
| `KAFKA_AUDIT_LOG_TOPIC` | `audit-log` | Audit log topic |
| `REDIS_HOST` | `localhost` | Redis host |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_DB` | `0` | Redis DB |
| `REDIS_REPLAY_TTL_HOURS` | `24` | TTL for replay seen set (hours) |
| `REDIS_QUARANTINE_TTL_SECONDS` | `3600` | Quarantine rule TTL (1 hour) |

## Project structure

```
rtace/
├── simulator/           # Transaction event generator
├── detection_engine/    # Replay detector, tx-events → detections
├── containment_engine/ # Detections → Redis rules + audit-log
├── api/                 # FastAPI control API
├── common/              # Models, Kafka/Redis clients
├── configs/             # Kafka and Redis config
├── deployment/          # docker-compose.yml, prometheus.yml, grafana/provisioning
└── README.md
```

## Replay detection (first version)

- Each transaction is hashed (user, amount, merchant, timestamp, location).
- Hashes are stored in Redis set `replay:seen:{date}` with 24h TTL.
- If the same hash is seen again within the window → **replay_attack** detection is emitted to `detections`.
- Containment for replay: key `enforce:quarantine:user:{user_id}` is set in Redis with TTL 1 hour.

## License

Internal use. Adjust as needed for your organization.
