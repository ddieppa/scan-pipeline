#!/usr/bin/env python3
"""Cron entry point for scan-pipeline-v3.

Checks the scan inbox, processes new files, and outputs a JSON summary
that the cron agentTurn can use to present proposals to Daniel.

Also provides an approve subcommand that moves approved files and
updates all tracking state (proposals.json, scan_history.db).

Usage:
    python3 scripts/cron_scan.py [--inbox /path/to/inbox] [--dry-run]
    python3 scripts/cron_scan.py scan [--max-files N]
    python3 scripts/cron_scan.py approve [--batch BATCH_ID] [--all] [--sha SHA]...
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Ensure the pipeline root is on the path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.pipeline import process_batch
from app.settings import load_settings
from app.state.scan_db import init_db, log_file_move, update_result_status
from app.state.store import StateStore


def parse_args():
    parser = argparse.ArgumentParser(description="Cron scan inbox processor (v3)")
    subparsers = parser.add_subparsers(dest="command", help="Sub-command")

    # Scan subcommand (also default for backward compat)
    scan_parser = subparsers.add_parser("scan", help="Scan inbox and process files")
    scan_parser.add_argument("--inbox", default=None, help="Override inbox directory")
    scan_parser.add_argument("--dry-run", action="store_true", help="Only list files, don't process")
    scan_parser.add_argument("--max-files", type=int, default=20, help="Max files to process per run")
    scan_parser.add_argument("--files", nargs="+", default=None, help="Process specific files instead of scanning inbox")

    # Approve subcommand
    approve_parser = subparsers.add_parser("approve", help="Approve and move proposed files")
    approve_parser.add_argument("--batch", default=None, help="Batch ID to approve (latest if omitted)")
    approve_parser.add_argument("--all", action="store_true", help="Approve all pending files in the batch")
    approve_parser.add_argument("--sha", nargs="+", default=None, help="Approve specific SHA256 hashes")
    approve_parser.add_argument("--dry-run", action="store_true", help="Preview what would be moved without moving")
    approve_parser.add_argument("--dest-root", default=None, help="Override destination root")

    return parser.parse_args()


def collect_inbox_files(inbox: Path, supported_extensions: set[str]) -> list[Path]:
    """Walk the inbox recursively for supported file types (skip archives)."""
    archive_extensions = {".zip", ".7z", ".rar", ".tar", ".gz"}
    files = []
    for f in inbox.rglob("*"):
        try:
            if not f.is_file():
                continue
        except OSError:
            continue  # broken symlink or inaccessible
        ext = f.suffix.lower()
        if ext in archive_extensions:
            continue
        if ext in supported_extensions:
            files.append(f)
    def _safe_mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    files.sort(key=_safe_mtime)
    return files


def cmd_scan(args) -> int:
    """Run the scan command."""
    settings = load_settings()

    if args.inbox:
        import os
        os.environ["SCAN_INBOX"] = args.inbox
        settings = load_settings()

    inbox = settings.inbox_root
    if not inbox.is_dir():
        print(json.dumps({"status": "error", "error": f"Inbox not found: {inbox}"}))
        return 1

    if args.files:
        files = [Path(f) for f in args.files if Path(f).is_file()]
        if not files:
            print(json.dumps({"status": "error", "error": "No valid files found"}))
            return 1
    else:
        from app.classify.config import load_yaml_config
        file_type_config = load_yaml_config(settings.file_types_path)
        supported = {ext.lower() for ext in file_type_config.get("supported_extensions", [])}
        files = collect_inbox_files(inbox, supported)

    if not files:
        print("HEARTBEAT_OK")
        return 0

    if len(files) > args.max_files:
        print(f"⚠️ Found {len(files)} files, processing first {args.max_files}")
        files = files[:args.max_files]

    if args.dry_run:
        result = {
            "status": "dry_run",
            "inbox": str(inbox),
            "file_count": len(files),
            "files": [{"name": f.name, "path": str(f), "size_mb": round(f.stat().st_size / 1024 / 1024, 2)} for f in files],
        }
        print(json.dumps(result, indent=2, default=str))
        return 0

    try:
        result = process_batch(settings, files)
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc), "errorType": type(exc).__name__}, indent=2))
        return 1

    # Build summary
    successes = result.get("successes", 0)
    failures = result.get("failures", 0)
    batch_id = result.get("batchId", "unknown")
    results_list = result.get("results", [])

    summary_lines = [f"📁 Scan batch `{batch_id}`: {successes} processed, {failures} failed\n"]

    for i, item in enumerate(results_list, 1):
        status = item.get("status", "unknown")
        if status != "success":
            summary_lines.append(f"  {i}. ❌ {item.get('filename', '?')} — ERROR: {item.get('error', 'unknown')}")
            continue

        conf = item.get("confidence", 0)
        conf_pct = f"{conf * 100:.0f}%" if conf <= 1 else f"{conf:.0f}%"
        name = item.get("proposedName", "?")
        dest = item.get("proposedDest", "?")
        person = item.get("person", "?")
        doc_type = item.get("docType", "?")
        side_info = ""
        if item.get("side"):
            side_info = f" [{item['side']}]"
        if item.get("ambiguous"):
            side_info += f" ⚠️ AMBIGUOUS: {item.get('question', '')}"

        summary_lines.append(f"  {i}. 📄 `{name}` → `{dest}` ({conf_pct}, {person}, {doc_type}{side_info})")

    summary = "\n".join(summary_lines)

    # Save batch result
    state_output = settings.state_dir / "last_batch.json"
    state_output.parent.mkdir(parents=True, exist_ok=True)
    with state_output.open("w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str, ensure_ascii=False)

    print(summary)
    print(f"\n💾 Full results saved to: {state_output}")
    return 0 if failures == 0 else 1


def _find_proposals_for_batch(proposals_data: dict, batch_id: str | None) -> list[tuple[str, dict]]:
    """Return list of (sha256, proposal) tuples matching the batch."""
    if batch_id:
        items = [(sha, p) for sha, p in proposals_data.items() if p.get("batchId") == batch_id]
    else:
        # If no batch specified, get the latest batch by timestamp
        sorted_by_time = sorted(
            proposals_data.items(),
            key=lambda x: x[1].get("timestamp", ""),
            reverse=True,
        )
        if sorted_by_time:
            latest_batch = sorted_by_time[0][1].get("batchId")
            items = [(sha, p) for sha, p in proposals_data.items() if p.get("batchId") == latest_batch]
        else:
            items = []
    return items


def cmd_approve(args) -> int:
    """Approve and move proposed files, updating all tracking state."""
    settings = load_settings()
    state_dir = settings.state_dir
    store = StateStore(state_dir, settings.rule_suggestions_path)

    # Resolve destination root
    dest_root = Path(args.dest_root) if args.dest_root else Path("/mnt/e/QSync")
    if not dest_root.exists():
        print(json.dumps({"status": "error", "error": f"Dest root not found: {dest_root}"}))
        return 1

    # Load proposals
    proposals_data = store._load_json(store.proposals_path, {})

    if not proposals_data:
        print("⚠️ No proposals found to approve.")
        return 0

    # Filter to the target batch
    batch_items = _find_proposals_for_batch(proposals_data, args.batch)

    if not batch_items:
        print(f"⚠️ No proposals found for batch: {args.batch or '(latest)'}")
        return 0

    # Determine which items to approve
    if args.all:
        target_items = [(sha, p) for sha, p in batch_items if p.get("status") == "pending"]
    elif args.sha:
        target_items = [(sha, p) for sha, p in batch_items if sha in args.sha and p.get("status") == "pending"]
    else:
        print("⚠️ No files selected. Use --all or --sha SHA1 SHA2...")
        return 0

    if not target_items:
        print("⚠️ No pending proposals match the selection criteria.")
        return 0

    # Initialize DB
    from app.state.scan_db import init_db as _init_db, log_file_move, update_result_status_by_path
    _init_db()

    moved_count = 0
    skipped_count = 0
    error_count = 0
    lines = [f"📝 Approving {len(target_items)} file(s) from batch: {args.batch or '(latest)'}\n"]

    for sha256, proposal in target_items:
        path_str = proposal.get("path", "")
        proposal_detail = proposal.get("proposal", {})
        proposed_name = proposal_detail.get("proposedName", "")
        proposed_dest = proposal_detail.get("proposedDest", "")

        src = Path(path_str)
        if not src.exists():
            lines.append(f"  ❌ SKIP (not found): {proposed_name}")
            skipped_count += 1
            continue

        # Build destination path
        dest_dir = dest_root / proposed_dest
        dest_file = dest_dir / proposed_name

        if dest_file.exists():
            lines.append(f"  ⚠️ SKIP (exists at dest): {proposed_name}")
            skipped_count += 1
            continue

        if args.dry_run:
            lines.append(f"  [DRY-RUN] {src.name} → {dest_file}")
            continue

        # Execute the move
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dest_file))
            lines.append(f"  ✅ {proposed_name} → {proposed_dest}")
            moved_count += 1

            # Clean up empty parent folders left behind in the inbox
            _cleanup_inbox_parents(src.parent, settings.inbox_root)

            # Update proposal status
            proposal["status"] = "approved"
            proposal["approved_at"] = datetime.now().isoformat(timespec="seconds")
            store.save_proposal(sha256, proposal)

            # Update DB: use file_path as key
            update_result_status_by_path(str(src), "approved")
            log_file_move(str(src), str(dest_file), success=True)

        except Exception as exc:
            lines.append(f"  ❌ ERROR moving {src.name}: {exc}")
            error_count += 1

            # Update DB with failure
            update_result_status_by_path(str(src), "error")
            log_file_move(str(src), str(dest_file), success=False, error=str(exc))

    summary = "\n".join(lines)
    print(summary)
    print(f"\n📊 Moved: {moved_count}, Skipped: {skipped_count}, Errors: {error_count}")

    return 0


def _cleanup_inbox_parents(start_dir: Path, inbox_root: Path) -> list[str]:
    """Remove empty directories from start_dir up to (but not including) inbox_root."""
    removed = []
    current = start_dir.resolve()
    stop = inbox_root.resolve()
    while current != stop and current.is_dir():
        try:
            if not any(current.iterdir()):
                current.rmdir()
                removed.append(str(current))
                current = current.parent
            else:
                break
        except OSError:
            break
    return removed


def main() -> int:
    args = parse_args()

    # Default to scan for backward compatibility
    if not args.command:
        return cmd_scan(args)

    if args.command == "scan":
        return cmd_scan(args)
    elif args.command == "approve":
        return cmd_approve(args)
    else:
        print(f"Unknown command: {args.command}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
