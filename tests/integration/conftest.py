"""Integration tests run only against real infrastructure.

Enable with RTACE_INTEGRATION=1 and a reachable Kafka + Redis:

    RTACE_INTEGRATION=1 KAFKA_BOOTSTRAP_SERVERS=localhost:9092 REDIS_HOST=localhost \
        pytest tests/integration -v

They use a dedicated Redis DB and unique per-run Kafka topics, so they do not
disturb a running stack. The decision-service test additionally needs
RTACE_DECISION_URL (e.g. http://localhost:8090/v1/decide).
"""

import os

import pytest

if os.getenv("RTACE_INTEGRATION") != "1":
    pytest.skip("set RTACE_INTEGRATION=1 to run live integration tests", allow_module_level=True)
