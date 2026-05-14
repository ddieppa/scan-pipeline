"""Safe file move with pre-flight checks and failure tracking.

Handles the critical path of moving approved files from processing/ to QSync.
Includes:
- Mount check: verifies /mnt/e/QSync is accessible
- Space check: verifies enough disk space at destination
- Atomic copy+rename: source survives until dest is durable
- Retry logic: up to 3 attempts with backoff
- Failure logging: all failed moves tracked in move_log table
- Lifecycle update: marks move_failed on failure
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Minimum free space required at destination (50 MB)
MIN_FREE_SPACE_MB = 50


def check_qsync_mount(qsync_root: Path) -> tuple[bool, str]:
    """Verify QSync mount is accessible and writable.

    Returns (ok, reason) tuple.
    """
    if not qsync_root.exists():
        return False, f"QSync root does not exist: {qsync_root}"

    # Check it's actually a mount point or accessible directory
    if not qsync_root.is_dir():
        return False, f"QSync root is not a directory: {qsync_root}"

    # Try to write a test file
    test_file = qsync_root / ".scan_pipeline_write_test"
    try:
        test_file.write_text("write-test")
        test_file.unlink()
    except (OSError, PermissionError) as e:
        return False, f"QSync root is not writable: {e}"

    return True, "ok"


def check_disk_space(dest_dir: Path, file_size: int, min_free_mb: int = MIN_FREE_SPACE_MB) -> tuple[bool, str]:
    """Check if destination has enough disk space.

    Returns (ok, reason) tuple.
    """
    try:
        usage = shutil.disk_usage(str(dest_dir))
        free_mb = usage.free / (1024 * 1024)
        needed_mb = (file_size / (1024 * 1024)) + min_free_mb
        if free_mb < needed_mb:
            return False, f"Insufficient disk space: {free_mb:.0f} MB free, need {needed_mb:.0f} MB"
    except OSError as e:
        return False, f"Could not check disk space: {e}"

    return True, "ok"


def safe_move_file(
    source: Path,
    target_path: Path,
    max_retries: int = 3,
    retry_delay: float = 2.0,
    copy_mode: bool = False,
) -> dict[str, Any]:
    """Safely move a file with pre-flight checks and atomic write.

    Strategy: copy to temp file in target dir, then os.rename (atomic on same
    filesystem). Only remove source after rename succeeds.

    Args:
        source: Source file path (must exist)
        target_path: Destination path (directory must exist or will be created)
        max_retries: Maximum number of retry attempts
        retry_delay: Seconds between retries
        copy_mode: If True, keep source file after copy

    Returns:
        Dict with keys: ok, moved_to, error, attempts, sha256_verified
    """
    result: dict[str, Any] = {
        "ok": False,
        "moved_to": None,
        "error": None,
        "attempts": 0,
        "sha256_verified": False,
    }

    if not source.exists():
        result["error"] = f"Source file does not exist: {source}"
        return result

    # Pre-flight: source file size
    try:
        file_size = source.stat().st_size
    except OSError as e:
        result["error"] = f"Cannot stat source file: {e}"
        return result

    # Pre-flight: QSync mount check
    qsync_root = _find_qsync_root(target_path)
    mount_ok, mount_reason = check_qsync_mount(qsync_root)
    if not mount_ok:
        # If mount is down, don't even try - leave file in processing/
        result["error"] = f"QSync mount check failed: {mount_reason}"
        _log_move_failure(source, target_path, mount_reason, attempt=0)
        return result

    # Pre-flight: disk space check
    target_dir = target_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    space_ok, space_reason = check_disk_space(target_dir, file_size)
    if not space_ok:
        result["error"] = f"Disk space check failed: {space_reason}"
        _log_move_failure(source, target_path, space_reason, attempt=0)
        return result

    # Pre-flight: compute source hash for verification
    source_hash = _sha256_file(source)

    # Retry loop
    last_error = None
    for attempt in range(1, max_retries + 1):
        result["attempts"] = attempt

        try:
            # Step 1: Copy to temp file in target directory
            # Using target_dir ensures same filesystem for the final rename
            temp_name = f".scan_pipeline_tmp_{source.name}_{attempt}"
            temp_path = target_dir / temp_name

            logger.info(f"Copy attempt {attempt}: {source} -> {temp_path}")
            shutil.copy2(str(source), str(temp_path))

            # Step 2: Verify copied file integrity
            temp_hash = _sha256_file(temp_path)
            if temp_hash != source_hash:
                temp_path.unlink(missing_ok=True)
                last_error = f"SHA256 mismatch after copy (attempt {attempt})"
                logger.warning(last_error)
                time.sleep(retry_delay * attempt)
                continue

            # Step 3: Atomic rename to final destination
            # os.rename is atomic on same filesystem
            if target_path.exists():
                # Target already exists - use unique suffix
                target_path = _unique_path(target_path)

            os.rename(str(temp_path), str(target_path))

            # Step 4: Verify final file exists and hash matches
            if not target_path.exists():
                # Rename might have failed silently
                last_error = f"Target file does not exist after rename (attempt {attempt})"
                logger.warning(last_error)
                time.sleep(retry_delay * attempt)
                continue

            final_hash = _sha256_file(target_path)
            if final_hash != source_hash:
                # Hash mismatch - this shouldn't happen after atomic rename
                logger.error(f"Final hash mismatch! Source: {source_hash}, Target: {final_hash}")
                # Don't delete target - it might be the only copy
                last_error = f"Final SHA256 mismatch (attempt {attempt})"
                time.sleep(retry_delay * attempt)
                continue

            # Step 5: Move sidecars with the main file
            _move_sidecars(source, target_path, copy_mode=copy_mode)

            # Step 6: Remove source (only after verified copy + sidecars)
            if not copy_mode:
                try:
                    source.unlink()
                    logger.info(f"Removed source: {source}")
                except OSError as e:
                    # Source removal failed but target is good — log but don't fail
                    logger.warning(f"Could not remove source {source}: {e}")

            # Success!
            result["ok"] = True
            result["moved_to"] = str(target_path)
            result["sha256_verified"] = True
            return result

        except OSError as e:
            # Clean up temp file if it exists
            temp_path = target_dir / f".scan_pipeline_tmp_{source.name}_{attempt}"
            temp_path.unlink(missing_ok=True)
            last_error = f"OSError on attempt {attempt}: {e}"
            logger.warning(last_error)
            time.sleep(retry_delay * attempt)

        except Exception as e:
            temp_path = target_dir / f".scan_pipeline_tmp_{source.name}_{attempt}"
            temp_path.unlink(missing_ok=True)
            last_error = f"Unexpected error on attempt {attempt}: {e}"
            logger.error(last_error)
            time.sleep(retry_delay * attempt)

    # All retries failed
    result["error"] = last_error or "All retries exhausted"
    _log_move_failure(source, target_path, result["error"], attempt=max_retries)
    return result


def _find_qsync_root(path: Path) -> Path:
    """Walk up the path to find the QSync root (contains 02-Areas, etc)."""
    current = path
    for _ in range(10):  # Don't walk more than 10 levels
        if current.exists() and (current / "02-Areas").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    # Fallback: return the original path's parent chain up to /mnt/e
    for p in path.parents:
        if str(p).startswith("/mnt/e") and p.name in ("QSync", "Qsync-Scanned-Documents"):
            return p
    return path.parent.parent  # Best guess


def _unique_path(path: Path) -> Path:
    """Generate a unique path by adding version suffixes."""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    index = 2
    while True:
        candidate = path.with_name(f"{stem}_v{index}{suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def _sha256_file(path: Path) -> str:
    """Compute SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _log_move_failure(source: Path, target: Path, reason: str, attempt: int):
    """Log a move failure to the move_failed table in scan_history.db.

    This creates the table lazily if it doesn't exist, so it works even
    if init_db hasn't been called yet.
    """
    try:
        import sqlite3
        from app.state.scan_db import DB_PATH

        conn = sqlite3.connect(str(DB_PATH))
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS move_failed (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                target_path TEXT NOT NULL,
                reason TEXT NOT NULL,
                attempt INTEGER,
                failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                recovered_at TIMESTAMP,
                recovery_action TEXT
            )
        """)
        c.execute("""
            INSERT INTO move_failed (source_path, target_path, reason, attempt)
            VALUES (?, ?, ?, ?)
        """, (str(source), str(target), reason, attempt))
        conn.commit()
        conn.close()
    except Exception as e:
        # Don't let logging failures break the move
        logger.error(f"Failed to log move failure: {e}")


def _move_sidecars(source: Path, target_path: Path, copy_mode: bool = False) -> list[Path]:
    """Move or copy .meta.json and .ocr.txt sidecars alongside the main file.

    Ensures atomicity: sidecars travel with the main file. If any sidecar
    move fails, the error is logged but does not block the main move.
    """
    moved = []
    for suffix in [".meta.json", ".ocr.txt"]:
        src = source.parent / f"{source.stem}{suffix}"
        if not src.exists():
            continue
        dst = target_path.parent / f"{target_path.stem}{suffix}"
        try:
            if copy_mode:
                shutil.copy2(str(src), str(dst))
            else:
                shutil.move(str(src), str(dst))
            moved.append(dst)
        except OSError as e:
            logger.warning(f"Sidecar move failed for {src.name}: {e}")
    return moved


def list_failed_moves() -> list[dict]:
    """List all unrecovered move failures from the database."""
    try:
        import sqlite3
        from app.state.scan_db import DB_PATH

        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
            SELECT * FROM move_failed
            WHERE recovered_at IS NULL
            ORDER BY failed_at DESC
        """)
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def recover_failed_move(failure_id: int, qsync_root: Path | None = None) -> dict[str, Any]:
    """Retry a failed move from the move_failed table.

    Args:
        failure_id: ID from move_failed table
        qsync_root: Override QSync root (defaults to /mnt/e/QSync)
    """
    try:
        import sqlite3
        from app.state.scan_db import DB_PATH

        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM move_failed WHERE id = ?", (failure_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return {"ok": False, "error": f"No move failure with id {failure_id}"}

        source = Path(row["source_path"])
        target = Path(row["target_path"])

        if not source.exists():
            conn.close()
            return {"ok": False, "error": f"Source file no longer exists: {source}"}

        # Retry the move
        result = safe_move_file(source, target)

        if result["ok"]:
            c.execute("""
                UPDATE move_failed
                SET recovered_at = CURRENT_TIMESTAMP, recovery_action = 'retried'
                WHERE id = ?
            """, (failure_id,))
            conn.commit()

        conn.close()
        return result

    except Exception as e:
        return {"ok": False, "error": str(e)}