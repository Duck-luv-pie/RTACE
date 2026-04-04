"""Detection engine: consume tx-events and auth-events, run detectors, publish detections."""

import logging
import time

from prometheus_client import start_http_server

from common.kafka_client import create_consumer, create_producer, send_message
from common.metrics import (
    credential_stuffing_detections_total,
    detection_pipeline_latency_seconds,
    fraud_burst_detections_total,
    geo_velocity_detections_total,
    replay_detections_total,
    transactions_processed_total,
)
from common.models import AuthEvent, TransactionEvent
from common.redis_client import create_redis_client
from configs.kafka_config import KafkaConfig
from detection_engine.credential_stuffing_detector import check_credential_stuffing
from detection_engine.fraud_burst_detector import check_fraud_burst
from detection_engine.geo_velocity_detector import check_geo_velocity
from detection_engine.replay_detector import check_replay

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

METRICS_PORT = 9091


def run_detection_engine() -> None:
    """Consume tx-events and auth-events, run detectors, produce to detections topic."""
    start_http_server(METRICS_PORT)
    logger.info("Metrics exposed on port %s", METRICS_PORT)

    kafka_config = KafkaConfig.from_env()
    consumer = create_consumer(
        [kafka_config.tx_events_topic, kafka_config.auth_events_topic],
        group_id="rtace-detection-engine",
        config=kafka_config,
    )
    producer = create_producer(kafka_config)
    redis_client = create_redis_client()

    logger.info(
        "Detection engine started: consume %s + %s → %s",
        kafka_config.tx_events_topic,
        kafka_config.auth_events_topic,
        kafka_config.detections_topic,
    )

    for message in consumer:
        start = time.perf_counter()
        try:
            raw = message.value
            if not raw:
                continue
            topic = message.topic

            if topic == kafka_config.auth_events_topic:
                auth = AuthEvent.model_validate(raw)
                det_user, det_ip = check_credential_stuffing(auth, redis_client)
                for det, scope in (
                    (det_user, "user"),
                    (det_ip, "ip"),
                ):
                    if det:
                        credential_stuffing_detections_total.labels(scope=scope).inc()
                        send_message(
                            producer,
                            kafka_config.detections_topic,
                            det.model_dump(mode="json"),
                            key=det.user_id,
                        )
                        logger.info(
                            "Credential stuffing (%s scope): user_id=%s ip=%s detection_id=%s",
                            scope,
                            det.user_id,
                            det.ip_address,
                            det.detection_id,
                        )
            else:
                tx = TransactionEvent.model_validate(raw)
                replay_detection = check_replay(tx, redis_client)
                geo_detection = check_geo_velocity(tx, redis_client)
                burst_detection = check_fraud_burst(tx, redis_client)

                if replay_detection:
                    replay_detections_total.labels(
                        detection_type=replay_detection.detection_type
                    ).inc()
                    send_message(
                        producer,
                        kafka_config.detections_topic,
                        replay_detection.model_dump(mode="json"),
                        key=replay_detection.user_id,
                    )
                    logger.info(
                        "Replay detected: user_id=%s transaction_id=%s detection_id=%s",
                        replay_detection.user_id,
                        replay_detection.transaction_id,
                        replay_detection.detection_id,
                    )

                if geo_detection:
                    geo_velocity_detections_total.labels(
                        detection_type=geo_detection.detection_type
                    ).inc()
                    send_message(
                        producer,
                        kafka_config.detections_topic,
                        geo_detection.model_dump(mode="json"),
                        key=geo_detection.user_id,
                    )
                    logger.info(
                        "Geo velocity anomaly: user_id=%s transaction_id=%s detection_id=%s",
                        geo_detection.user_id,
                        geo_detection.transaction_id,
                        geo_detection.detection_id,
                    )

                if burst_detection:
                    fraud_burst_detections_total.labels(
                        detection_type=burst_detection.detection_type
                    ).inc()
                    send_message(
                        producer,
                        kafka_config.detections_topic,
                        burst_detection.model_dump(mode="json"),
                        key=burst_detection.user_id,
                    )
                    logger.info(
                        "Fraud burst: user_id=%s transaction_id=%s detection_id=%s",
                        burst_detection.user_id,
                        burst_detection.transaction_id,
                        burst_detection.detection_id,
                    )

                outcome = (
                    "detected"
                    if (replay_detection or geo_detection or burst_detection)
                    else "clean"
                )
                transactions_processed_total.labels(outcome=outcome).inc()

            detection_pipeline_latency_seconds.observe(time.perf_counter() - start)
        except Exception as e:
            logger.exception("Error processing message: %s", e)
            detection_pipeline_latency_seconds.observe(time.perf_counter() - start)

    consumer.close()
    producer.close()


if __name__ == "__main__":
    run_detection_engine()
