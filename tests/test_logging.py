from __future__ import annotations

from app.logging import configure_logging, get_logger


def test_configure_logging_and_get_logger_smoke() -> None:
    configure_logging()
    logger = get_logger("test")
    logger.info("smoke_test_event", incident_id="abc123")
