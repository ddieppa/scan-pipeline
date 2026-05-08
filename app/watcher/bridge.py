from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable
from uuid import uuid4

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:
    class FileSystemEvent:  # type: ignore[override]
        def __init__(self, src_path: str = "", is_directory: bool = False) -> None:
            self.src_path = src_path
            self.is_directory = is_directory

    class FileSystemEventHandler:  # type: ignore[override]
        pass

    class Observer:  # type: ignore[override]
        def schedule(self, *args, **kwargs) -> None:
            raise RuntimeError("watchdog is required to run the file watcher")

        def start(self) -> None:
            raise RuntimeError("watchdog is required to run the file watcher")

        def stop(self) -> None:
            return None

        def join(self) -> None:
            return None

from app.settings import Settings
from app.utils import SUPPORTED_EXTENSIONS


def post_hook_payload(url: str, secret: str, payload: dict) -> dict:
    """POST webhook payload with 3 retries and exponential backoff (1s, 4s, 16s).
    Logs every attempt. On final failure, writes to notification_log table.
    """
    import logging
    logger = logging.getLogger(__name__)
    max_retries = 3
    backoff_base = 1  # seconds; delays: 1, 4, 16
    payload_preview = json.dumps(payload)[:200]
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
            request.add_header("Authorization", f"Bearer {secret}")
            request.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                result = json.loads(body) if body else {"ok": True}
                logger.info("Webhook sent", extra={
                    "phase": "webhook",
                    "duration_ms": attempt,
                    "sha256": payload.get("metadata", {}).get("batchId", ""),
                })
                # Log successful send
                _log_notification(
                    sha256=payload.get("metadata", {}).get("batchId", ""),
                    channel="webhook",
                    target=url,
                    status="sent" if attempt == 1 else "retried",
                    attempt=attempt,
                    error=None,
                    payload_preview=payload_preview,
                )
                return result
        except Exception as exc:
            last_error = str(exc)
            delay = backoff_base ** (2 ** (attempt - 1))  # 1, 4, 16
            logger.warning(f"Webhook attempt {attempt}/{max_retries} failed: {exc}", extra={
                "phase": "webhook",
                "sha256": payload.get("metadata", {}).get("batchId", ""),
            })
            if attempt < max_retries:
                time.sleep(delay)

    # All retries exhausted — log final failure
    logger.error(f"Webhook failed after {max_retries} retries: {last_error}", extra={
        "phase": "webhook",
        "sha256": payload.get("metadata", {}).get("batchId", ""),
    })
    _log_notification(
        sha256=payload.get("metadata", {}).get("batchId", ""),
        channel="webhook",
        target=url,
        status="failed",
        attempt=max_retries,
        error=last_error,
        payload_preview=payload_preview,
    )
    raise RuntimeError(f"Webhook failed after {max_retries} retries: {last_error}")


def _log_notification(sha256: str, channel: str, target: str, status: str,
                     attempt: int, error: str | None, payload_preview: str) -> None:
    """Write a notification attempt to the notification_log table."""
    try:
        from app.state.scan_db import log_notification
        log_notification(sha256, channel, target, status, attempt, error, payload_preview)
    except Exception:
        pass  # Don't fail the caller if DB logging fails


def build_openclaw_payload(settings: Settings, batch_id: str, files: list[str]) -> dict:
    """Build webhook payload to create a TaskFlow for scan batch."""
    return {
        "action": "create_flow",
        "goal": f"[SCAN v3] Process new files: {json.dumps(files)}",
        "status": "queued",
        "notifyPolicy": "done_only",
        "metadata": {
            "batchId": batch_id,
            "files": files,
            "source": "scan-pipeline-v3",
        }
    }


class StableFileBatcher:
    def __init__(
        self,
        settings: Settings,
        debounce_seconds: float,
        batch_window_seconds: float,
        stable_poll_interval_seconds: float,
        stable_checks: int,
        sender: Callable[[str, str, dict], dict] = post_hook_payload,
    ) -> None:
        self.settings = settings
        self.debounce_seconds = debounce_seconds
        self.batch_window_seconds = batch_window_seconds
        self.stable_poll_interval_seconds = stable_poll_interval_seconds
        self.stable_checks = stable_checks
        self.sender = sender
        self._lock = threading.Lock()
        self._pending: dict[Path, float] = {}
        self._timer: threading.Timer | None = None

    def queue(self, path: Path) -> None:
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return
        with self._lock:
            self._pending[path.resolve()] = time.time()
            self._arm_timer()

    def flush(self) -> dict | None:
        with self._lock:
            candidates = list(self._pending.keys())
            self._pending.clear()
            self._timer = None
        stable_files = [str(path) for path in candidates if self._is_stable(path)]
        if not stable_files or not self.settings.webhook_url or not self.settings.webhook_secret:
            return None
        batch_id = f"watch-{uuid4().hex[:12]}"
        payload = build_openclaw_payload(self.settings, batch_id, stable_files)
        return self.sender(self.settings.webhook_url, self.settings.webhook_secret, payload)

    def _arm_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        delay = max(self.debounce_seconds, self.batch_window_seconds)
        self._timer = threading.Timer(delay, self.flush)
        self._timer.daemon = True
        self._timer.start()

    def _is_stable(self, path: Path) -> bool:
        if not path.exists() or not path.is_file():
            return False
        previous_size = None
        stable_hits = 0
        for _ in range(max(2, self.stable_checks + 1)):
            try:
                size = path.stat().st_size
                with path.open("rb"):
                    pass
            except OSError:
                return False
            if previous_size is not None and size == previous_size:
                stable_hits += 1
            else:
                stable_hits = 0
            if stable_hits >= self.stable_checks:
                return True
            previous_size = size
            time.sleep(self.stable_poll_interval_seconds)
        return False


class ScanInboxHandler(FileSystemEventHandler):
    def __init__(self, batcher: StableFileBatcher) -> None:
        self.batcher = batcher

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.batcher.queue(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            destination = getattr(event, "dest_path", None)
            if destination:
                self.batcher.queue(Path(destination))


def run_watcher(settings: Settings, file_type_config: dict) -> None:
    watcher_config = file_type_config.get("watcher", {})
    batcher = StableFileBatcher(
        settings=settings,
        debounce_seconds=float(watcher_config.get("debounce_seconds", 3.0)),
        batch_window_seconds=float(watcher_config.get("batch_window_seconds", 5.0)),
        stable_poll_interval_seconds=float(watcher_config.get("stable_poll_interval_seconds", 1.0)),
        stable_checks=int(watcher_config.get("stable_checks", 2)),
    )
    handler = ScanInboxHandler(batcher)
    observer = Observer()
    observer.schedule(handler, str(settings.inbox_root), recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(1)
    finally:
        observer.stop()
        observer.join()
