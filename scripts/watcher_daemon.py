#!/usr/bin/env python3
"""Inbox watcher daemon — watches for new files, scans, proposes, and processes approvals.

Architecture:
  - watchdog observer monitors the inbox folder recursively
  - New files are debounced and batched (wait for stable writes)
  - On batch ready, runs the full v3 scan pipeline (OCR + classification)
  - Proposals are saved to state and presented for approval
  - Approval can come via:
    1. Telegram inline buttons (if configured)
    2. Webhook to OpenClaw (if configured)
    3. CLI approve command
  - Approved files are moved to QSync and empty inbox folders cleaned up

Usage:
    python3 scripts/watcher_daemon.py [--once] [--dry-run]

    --once     Process existing inbox files once, then exit
    --dry-run  Process but don't move files
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.classify.config import load_compiled_rules, load_yaml_config
from app.classify.engine import classify_document
from app.coordinator import approve_proposal, _cleanup_empty_parents
from app.extractors.docx import extract_docx
from app.extractors.images import extract_image
from app.extractors.pdf import extract_pdf
from app.extractors.xlsx import extract_xlsx
from app.settings import load_settings
from app.state.scan_db import init_db, log_file_move, update_result_status_by_path
from app.state.store import StateStore
from app.utils import normalize_spaces, safe_filename_component, scan_date_from_mtime, sha256_file

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
    from watchdog.observers.polling import PollingObserver
except ImportError:
    print("ERROR: watchdog is required. Install with: pip install watchdog")
    sys.exit(1)


# ── File collection and stability checking ────────────────────────

ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".gz"}


def collect_inbox_files(inbox: Path, supported: set[str]) -> list[Path]:
    """Walk the inbox recursively for supported file types."""
    files = []
    for f in inbox.rglob("*"):
        try:
            if not f.is_file():
                continue
        except OSError:
            continue
        ext = f.suffix.lower()
        if ext in ARCHIVE_EXTENSIONS:
            continue
        if ext in supported:
            files.append(f)
    return sorted(files)


def is_stable(path: Path, checks: int = 2, interval: float = 1.0) -> bool:
    """Check that a file's size hasn't changed for `checks` consecutive polls."""
    prev_size = None
    stable_hits = 0
    for _ in range(checks + 1):
        try:
            size = path.stat().st_size
            with path.open("rb"):
                pass
        except OSError:
            return False
        if prev_size is not None and size == prev_size:
            stable_hits += 1
        else:
            stable_hits = 0
        if stable_hits >= checks:
            return True
        prev_size = size
        time.sleep(interval)
    return False


# ── Scan processing ──────────────────────────────────────────────

def scan_file(path: Path, rules, file_type_config: dict) -> dict | None:
    """Extract text, classify, and return a proposal dict."""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            extraction = extract_pdf(
                path,
                inspect_pages=int(file_type_config.get("pdf", {}).get("inspect_pages", 10)),
                min_text_chars_before_skip_ocr=int(file_type_config.get("pdf", {}).get("min_text_chars_before_skip_ocr", 20)),
                render_dpi=int(file_type_config.get("pdf", {}).get("render_dpi", 200)),
            )
        elif ext in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}:
            extraction = extract_image(path, int(file_type_config.get("images", {}).get("max_ocr_chars", 20000)))
        elif ext == ".docx":
            extraction = extract_docx(path, 400, 200)
        elif ext == ".xlsx":
            extraction = extract_xlsx(path, 8, 300)
        else:
            return None
    except Exception as exc:
        return {"status": "error", "path": str(path), "error": str(exc)}

    sha = sha256_file(path)
    scan_date = scan_date_from_mtime(path)
    classification = classify_document(extraction.text, path, scan_date, rules)

    return {
        "status": "success",
        "sha256": sha,
        "path": str(path),
        "filename": path.name,
        "scanDate": scan_date,
        "textSource": extraction.text_source,
        "docType": classification.doc_type,
        "person": classification.person,
        "provider": classification.provider,
        "proposedName": classification.proposed_name,
        "proposedDest": classification.proposed_dest,
        "confidence": classification.confidence,
        "ruleMatchId": classification.rule_match_id,
        "ocrSample": normalize_spaces(extraction.text)[:240],
    }


def process_inbox(settings, rules, file_type_config, dry_run: bool = False) -> list[dict]:
    """Scan all inbox files, return proposals, and save to state."""
    supported = {e.lower() for e in file_type_config.get("supported_extensions", [])}
    inbox_files = collect_inbox_files(settings.inbox_root, supported)

    if not inbox_files:
        return []

    # Filter to only stable files
    stable_files = [f for f in inbox_files if is_stable(f)]

    if not stable_files:
        return []

    results = []
    for f in stable_files:
        result = scan_file(f, rules, file_type_config)
        if result and result.get("status") == "success":
            results.append(result)

    if not results:
        return results

    # Save proposals to state store
    store = StateStore(settings.state_dir, settings.rule_suggestions_path)
    batch_id = f"watch-{uuid4().hex[:12]}"

    for i, result in enumerate(results, 1):
        result["id"] = i
        store.save_proposal(
            result["sha256"],
            {
                "batchId": batch_id,
                "path": result["path"],
                "filename": result["filename"],
                "timestamp": datetime.now().isoformat(),
                "row_number": i,
                "proposal": {
                    "proposedName": result["proposedName"],
                    "proposedDest": result["proposedDest"],
                    "confidence": result["confidence"],
                    "docType": result["docType"],
                    "person": result["person"],
                    "provider": result["provider"],
                    "ruleMatchId": result["ruleMatchId"],
                },
                "status": "pending",
            },
        )

    # Save batch results
    batch_payload = {
        "status": "complete",
        "batchId": batch_id,
        "processed": len(results),
        "successes": len(results),
        "failures": 0,
        "results": results,
        "timestamp": datetime.now().isoformat(),
    }
    store.save_batch(batch_id, batch_payload)

    # Also save to last_batch.json for the cron scan script compatibility
    batch_output = settings.state_dir / "last_batch.json"
    batch_output.parent.mkdir(parents=True, exist_ok=True)
    with batch_output.open("w", encoding="utf-8") as f:
        json.dump(batch_payload, f, indent=2, default=str, ensure_ascii=False)

    # Save scan history
    try:
        init_db()
        from app.state.scan_db import save_scan_session
        save_scan_session(results, f"Watcher batch {batch_id}")
    except Exception:
        pass

    return results


def format_proposals(results: list[dict]) -> str:
    """Format scan results as a readable summary for messaging."""
    if not results:
        return "📭 Inbox is empty."

    lines = [f"📋 **{len(results)} new scan(s) found:**\n"]

    for i, r in enumerate(results, 1):
        conf = r.get("confidence", 0)
        conf_pct = f"{conf * 100:.0f}%" if conf <= 1 else f"{conf:.0f}%"
        name = r.get("proposedName", "?")
        dest = r.get("proposedDest", "?")
        person = r.get("person", "?")
        doc_type = r.get("docType", "?")

        lines.append(
            f"{i}. 📄 `{name}`\n"
            f"   → `{dest}`\n"
            f"   {person} | {doc_type} | {conf_pct}"
        )

    return "\n".join(lines)


def approve_and_move(settings, sha256: str, override_name: str | None = None, override_dest: str | None = None) -> dict:
    """Approve a proposal, move the file, and clean up empty inbox folders."""
    result = approve_proposal(settings, sha256, override_dest=override_dest, override_name=override_name)

    if result.get("ok"):
        # Update scan DB
        try:
            init_db()
            store = StateStore(settings.state_dir, settings.rule_suggestions_path)
            proposal = store.get_proposal(sha256)
            src = Path(proposal["path"]) if proposal else None
            if src:
                update_result_status_by_path(str(src), "approved")
                dest_path = Path(result["movedTo"])
                log_file_move(str(src), str(dest_path), success=True)
        except Exception:
            pass

    return result


# ── Watcher daemon ────────────────────────────────────────────────

class InboxBatcher:
    """Collects new file events and batches them for processing."""

    def __init__(self, debounce_seconds: float = 5.0, batch_window: float = 10.0,
                 stable_checks: int = 2, stable_interval: float = 1.0):
        self.debounce_seconds = debounce_seconds
        self.batch_window = batch_window
        self.stable_checks = stable_checks
        self.stable_interval = stable_interval
        self._lock = threading.Lock()
        self._pending: dict[Path, float] = {}
        self._timer: threading.Timer | None = None
        self._on_batch_ready = None  # callback set by watcher

    def set_callback(self, callback):
        self._on_batch_ready = callback

    def queue(self, path: Path) -> None:
        supported = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".docx", ".xlsx"}
        if path.suffix.lower() not in supported:
            return
        with self._lock:
            self._pending[path.resolve()] = time.time()
            self._arm_timer()

    def flush(self) -> None:
        with self._lock:
            self._pending.clear()
            self._timer = None
        if self._on_batch_ready:
            self._on_batch_ready()

    def _arm_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        delay = max(self.debounce_seconds, self.batch_window)
        self._timer = threading.Timer(delay, self.flush)
        self._timer.daemon = True
        self._timer.start()


class InboxHandler(FileSystemEventHandler):
    def __init__(self, batcher: InboxBatcher):
        self.batcher = batcher

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.batcher.queue(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            dest = getattr(event, "dest_path", None)
            if dest:
                self.batcher.queue(Path(dest))


def run_watcher(settings, rules, file_type_config, dry_run: bool = False):
    """Start the inbox watcher daemon."""
    print(f"👁️  Watching inbox: {settings.inbox_root}")
    print(f"📁 QSync root: {settings.qsync_root}")
    print(f"⏱️  Debounce: 5s | Batch window: 10s")
    print(f"{'🔍 DRY RUN — files will not be moved' if dry_run else '✅ Live mode'}")
    print("Press Ctrl+C to stop.\n")

    # Process existing files first
    _process_and_notify(settings, rules, file_type_config, dry_run)

    # Set up the watcher
    batcher = InboxBatcher(debounce_seconds=5.0, batch_window=10.0)
    batcher.set_callback(lambda: _process_and_notify(settings, rules, file_type_config, dry_run))

    handler = InboxHandler(batcher)
    # Use PollingObserver for WSL compatibility (inotify doesn't work on /mnt/ drives)
    observer = PollingObserver(timeout=30)
    observer.schedule(handler, str(settings.inbox_root), recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n⏹️  Stopping watcher...")
        observer.stop()
    observer.join()
    print("👋 Watcher stopped.")


def _process_and_notify(settings, rules, file_type_config, dry_run: bool = False):
    """Process inbox files and output results."""
    results = process_inbox(settings, rules, file_type_config, dry_run=dry_run)

    if not results:
        return

    summary = format_proposals(results)
    print(f"\n{summary}")
    print(f"\n💾 Proposals saved. Run `python3 scripts/cron_scan.py approve --all` to approve,")
    print(f"   or `python3 scripts/cron_scan.py approve --sha SHA1 SHA2...` for specific files.")

    # Wake OpenClaw agent by triggering the scan cron job
    try:
        import subprocess
        batch_id = results[0].get("batchId", "unknown") if results else "unknown"
        count = len(results)
        # Write a signal file so the agent knows the watcher found files
        signal_path = Path(settings.state_dir) / "watcher_signal.json"
        signal_data = {
            "batchId": batch_id,
            "count": count,
            "timestamp": datetime.now().isoformat(),
            "summary": summary,
        }
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        signal_path.write_text(json.dumps(signal_data, indent=2))
        # Trigger OpenClaw cron job to wake the agent
        result = subprocess.run(
            ["openclaw", "cron", "run", "bde66a5f-cc54-4c5a-aad3-9dc04625910d"],
            timeout=30,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"📡 Triggered OpenClaw scan processor cron job")
        else:
            print(f"⚠️  Cron trigger failed: {result.stderr.strip() or result.stdout.strip()}")
    except Exception as e:
        print(f"⚠️  Wake failed: {e}")
        # Results are still saved in state files for manual review


# ── Main ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Inbox watcher daemon")
    parser.add_argument("--once", action="store_true", help="Process existing files once, then exit")
    parser.add_argument("--dry-run", action="store_true", help="Process but don't move files")
    args = parser.parse_args()

    settings = load_settings()
    rules = load_compiled_rules(settings.scan_rules_path)
    file_type_config = load_yaml_config(settings.file_types_path)

    init_db()

    if args.once:
        results = process_inbox(settings, rules, file_type_config, dry_run=args.dry_run)
        if results:
            print(format_proposals(results))
        else:
            print("📭 Inbox is empty.")
        return

    run_watcher(settings, rules, file_type_config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()