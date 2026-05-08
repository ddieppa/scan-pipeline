"""Structured logging configuration with JSON format and rotating file handler."""

import logging
from logging.handlers import RotatingFileHandler
import json
from pathlib import Path


class StructuredFormatter(logging.Formatter):
    """Format log records as JSON with optional extra fields (sha256, phase, duration_ms)."""

    def format(self, record):
        log_entry = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "module": record.module,
            "msg": record.getMessage(),
        }
        if hasattr(record, 'sha256'):
            log_entry['sha256'] = record.sha256
        if hasattr(record, 'phase'):
            log_entry['phase'] = record.phase
        if hasattr(record, 'duration_ms'):
            log_entry['duration_ms'] = record.duration_ms
        if record.exc_info:
            log_entry['error'] = self.formatException(record.exc_info)
        return json.dumps(log_entry)


def setup_logging(state_dir: Path, level=logging.INFO):
    """Configure structured JSON logging with rotation + console output.

    Args:
        state_dir: Base directory; logs go to state_dir / "logs" / "pipeline.log"
        level: Logging level (default INFO)
    """
    log_dir = state_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    handler = RotatingFileHandler(log_dir / "pipeline.log", maxBytes=10 * 1024 * 1024, backupCount=5)
    handler.setFormatter(StructuredFormatter())
    root = logging.getLogger()
    root.setLevel(level)
    # Remove existing handlers to avoid duplicates on repeated calls
    root.handlers.clear()
    root.addHandler(handler)
    # Also add stderr handler for console
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter('%(levelname)s %(module)s: %(message)s'))
    root.addHandler(console)