#!/usr/bin/env python3
"""Scan workflow — single entry point for inbox scanning, classification, approval, and cleanup.

This is the canonical script for the scan pipeline. All other entry points
(cron jobs, daemon watchers, CLI) should call this script.

Usage:
    # Scan inbox and show proposals (no moves)
    python3 scan_workflow.py scan

    # Scan and approve ALL proposals (move + cleanup)
    python3 scan_workflow.py approve --all

    # Approve specific files by SHA
    python3 scan_workflow.py approve --sha SHA1 SHA2

    # Full interactive workflow: scan → propose → approve each
    python3 scan_workflow.py run

    # Full non-interactive: scan → approve all → move → cleanup
    python3 scan_workflow.py run --yes

    # Dry run (show what would happen, don't move)
    python3 scan_workflow.py run --dry-run

    # Override destination/name for a specific file
    python3 scan_workflow.py approve --sha SHA1 --dest "02-Areas/Legal/" --name "2025-01-01_Custom_Name.pdf"

    # Watch daemon mode (filesystem watcher, auto-process)
    python3 scan_workflow.py watch
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.classify.config import load_compiled_rules, load_yaml_config
from app.classify.engine import classify_document, group_sequential_pages
from app.coordinator import approve_proposal, _cleanup_empty_parents
from app.extractors.docx import extract_docx
from app.extractors.images import extract_image
from app.extractors.pdf import extract_pdf
from app.extractors.xlsx import extract_xlsx
from app.extractors.common import ExtractionResult
from app.settings import load_settings
from app.state.scan_db import init_db, log_file_move, update_result_status_by_path, get_ocr_cache, save_ocr_cache, delete_ocr_cache, get_ocr_cache_by_path, list_ocr_cache
from app.state.store import StateStore
from app.utils import normalize_spaces, safe_filename_component, scan_date_from_mtime, sha256_file


# ── Constants ─────────────────────────────────────────────────────

ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".gz"}


# ── Phase 1: Scan ─────────────────────────────────────────────────

def collect_inbox_files(inbox: Path, supported: set[str]) -> list[Path]:
    """Walk the inbox recursively for supported file types (skip archives)."""
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
    """Check that a file's size hasn't changed for `checks` consecutive polls.
    
    DEPRECATED for batch use — use batch_is_stable() instead for bulk checks.
    Kept for single-file compatibility.
    """
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


def batch_is_stable(files: list[Path], checks: int = 2, interval: float = 1.0) -> list[Path]:
    """Check stability of multiple files in batch — one sleep cycle for all files.
    
    Instead of sleeping per-file (which takes O(n*interval) time),
    this checks all files, sleeps once, then checks all again.
    Total time: O(checks * interval) regardless of file count.
    
    A file is stable if its size is unchanged for `checks` consecutive rounds.
    A file that throws OSError (locked, deleted, etc.) is excluded.
    
    Args:
        files: List of file paths to check.
        checks: Number of consecutive unchanged checks required.
        interval: Seconds between batch rounds.
    
    Returns:
        List of stable file paths.
    """
    # Track size history per file: {path: [size_round1, size_round2, ...]}
    file_sizes: dict[Path, list[int]] = {}
    
    for round_num in range(checks + 1):
        for f in list(file_sizes.keys()):  # iterate copy since we might delete
            try:
                size = f.stat().st_size
                with f.open("rb"):
                    pass  # verify file is readable
                file_sizes[f].append(size)
            except OSError:
                del file_sizes[f]  # file unavailable, drop it
                
        # First round: add all files
        if round_num == 0:
            for f in files:
                if f not in file_sizes:
                    try:
                        size = f.stat().st_size
                        with f.open("rb"):
                            pass
                        file_sizes[f] = [size]
                    except OSError:
                        pass  # skip unavailable files
        
        # Sleep between rounds (not after the last round)
        if round_num < checks:
            time.sleep(interval)
    
    # A file is stable if its last `checks` size readings are identical
    stable = []
    for f, sizes in file_sizes.items():
        if len(sizes) < checks + 1:
            continue  # not enough readings (was added late or had errors)
        # Check last `checks` consecutive readings are the same
        last_sizes = sizes[-(checks + 1):]
        if len(set(last_sizes)) == 1:
            stable.append(f)
    
    return stable


def scan_file(path: Path, rules, file_type_config: dict, force_ocr: bool = False) -> dict | None:
    """Extract text from a file, classify it, return a proposal dict.
    
    If force_ocr is False and an OCR cache entry exists for this file's SHA256,
    the cached text is used instead of re-running OCR.
    If force_ocr is True, OCR is re-run and the cache is updated.
    The result dict includes 'ocr_from_cache' (bool) to indicate cache usage.
    """
    sha = sha256_file(path)
    file_size = None
    try:
        file_size = path.stat().st_size
    except OSError:
        pass

    # ── Check OCR cache ──
    cached = get_ocr_cache(sha) if not force_ocr else None
    ocr_from_cache = False
    ocr_duration_ms = None

    if cached and cached.get("ocr_text"):
        # Use cached OCR text — skip extraction entirely
        extraction = ExtractionResult(
            text=cached["ocr_text"],
            text_source=cached.get("text_source", "ocr_cache"),
            pages_inspected=0,
            needs_ocr=False,
            metadata={},
        )
        ocr_from_cache = True
    else:
        # ── Run OCR / extraction ──
        ext = path.suffix.lower()
        t0 = time.time()
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
            return {"status": "error", "path": str(path), "filename": path.name, "error": str(exc)}
        ocr_duration_ms = int((time.time() - t0) * 1000)

        # ── Save to OCR cache ──
        save_ocr_cache(
            sha256=sha,
            file_path=str(path),
            filename=path.name,
            ocr_text=extraction.text,
            text_source=extraction.text_source,
            ocr_duration_ms=ocr_duration_ms,
            file_size=file_size,
        )

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
        "medication": classification.medication,
        "brandName": classification.brand_name,
        "ocrSample": normalize_spaces(extraction.text)[:240],
        "ocrFullText": extraction.text,
        "reason_for_visit": classification.reason_for_visit,
        "final_diagnosis": classification.final_diagnosis,
        "physician": classification.physician,
        "ocr_from_cache": ocr_from_cache,
        "ocr_duration_ms": ocr_duration_ms,
    }


def cmd_scan(args) -> list[dict]:
    """Scan inbox, classify files, save proposals to state. Returns list of results."""
    settings = load_settings()
    rules = load_compiled_rules(settings.scan_rules_path)
    file_type_config = load_yaml_config(settings.file_types_path)
    supported = {e.lower() for e in file_type_config.get("supported_extensions", [])}
    max_workers = settings.max_workers or 4

    init_db()

    # Check both inbox and processing directories
    inbox_files = collect_inbox_files(settings.inbox_root, supported)
    processing_dir = settings.inbox_root.parent / "processing"
    processing_files = []
    if processing_dir.exists():
        processing_files = collect_inbox_files(processing_dir, supported)
    
    # Combine and deduplicate
    all_files = list(dict.fromkeys(inbox_files + processing_files))
    if not all_files:
        print("📭 Inbox is empty.")
        return []
    
    inbox_files = all_files
    if processing_files:
        print(f"📂 Found {len(processing_files)} file(s) in processing dir.")

    # Batch stability check — O(checks * interval) regardless of file count
    print(f"🔍 Checking {len(inbox_files)} file(s) for stability...", flush=True)
    stable_files = batch_is_stable(inbox_files, checks=2, interval=1.0)
    if not stable_files:
        print("📭 No stable files ready for processing.")
        return []

    if len(inbox_files) != len(stable_files):
        print(f"⏳ {len(inbox_files) - len(stable_files)} file(s) still writing, skipping for now.")

    # Filter out files already in the state DB (already processed)
    store = StateStore(settings.state_dir, settings.rule_suggestions_path)
    new_files = []
    for f in stable_files:
        sha = sha256_file(f)
        existing = store.get_proposal(sha)
        if existing and existing.get("status") in ("approved", "denied", "skipped"):
            continue  # already handled
        new_files.append(f)
    
    if len(stable_files) != len(new_files):
        print(f"📋 {len(stable_files) - len(new_files)} file(s) already processed, skipping.", flush=True)
    
    if not new_files:
        print("📭 No new files to process.")
        return []
    
    force_ocr = getattr(args, 'force_ocr', False)
    print(f"🔄 Processing {len(new_files)} file(s) with {max_workers} worker(s){' (force re-OCR)' if force_ocr else ''}...", flush=True)

    # Parallel OCR + classification
    results = []
    errors = []
    cache_hits = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_file = {executor.submit(scan_file, f, rules, file_type_config, force_ocr): f for f in new_files}
        for future in as_completed(future_to_file):
            f = future_to_file[future]
            try:
                result = future.result(timeout=60)
            except Exception as exc:
                errors.append({"status": "error", "path": str(f), "filename": f.name, "error": str(exc)})
                print(f"  ❌ {f.name}: {exc}", flush=True)
                continue
            if result and result.get("status") == "success":
                if result.get("ocr_from_cache"):
                    cache_hits += 1
                    cache_tag = "📦"
                else:
                    cache_tag = "🔍"
                results.append(result)
                print(f"  {cache_tag} {f.name} → {result['proposedName']}", flush=True)
            elif result and result.get("status") == "error":
                errors.append(result)
                print(f"  ❌ {result['filename']}: {result['error']}", flush=True)
    elapsed = time.time() - t0

    if not results:
        print("📭 No files successfully processed.")
        return []

    # Group multi-page documents and add sequential suffixes
    results = group_sequential_pages(results)

    # Save proposals to state store
    batch_id = f"batch-{uuid4().hex[:12]}"

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

    # Save batch
    batch_payload = {
        "status": "complete",
        "batchId": batch_id,
        "processed": len(results),
        "successes": len(results),
        "failures": len(errors),
        "results": results,
        "timestamp": datetime.now().isoformat(),
    }
    store.save_batch(batch_id, batch_payload)

    # Save to last_batch.json
    batch_output = settings.state_dir / "last_batch.json"
    batch_output.parent.mkdir(parents=True, exist_ok=True)
    with batch_output.open("w", encoding="utf-8") as f:
        json.dump(batch_payload, f, indent=2, default=str, ensure_ascii=False)

    # Save scan history
    try:
        from app.state.scan_db import save_scan_session
        save_scan_session(results, f"Scan batch {batch_id}")
    except Exception:
        pass

    # Save last_scan_results.json
    scan_output = settings.state_dir / "last_scan_results.json"
    with scan_output.open("w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str, ensure_ascii=False)

    # Print summary
    print(format_proposals(results))
    cache_info = f"📦 {cache_hits} from cache, 🔍 {len(results) - cache_hits} freshly OCR'd" if cache_hits else f"🔍 All {len(results)} freshly OCR'd"
    print(f"\n⏱️  {len(results)} file(s) processed in {elapsed:.1f}s ({max_workers} workers) | {cache_info}")
    print(f"💾 Batch: {batch_id}")
    print(f"   Proposals saved to state. Use `approve` to move files.")
    print(f"   Use `ocr-cache show --sha SHA` to view cached OCR text.")
    print(f"   Use `ocr-cache rerun --sha SHA` to force re-OCR a specific file.")

    return results


# ── Phase 2: Approve ──────────────────────────────────────────────

def cmd_approve(args) -> None:
    """Approve pending proposals and move files."""
    settings = load_settings()
    store = StateStore(settings.state_dir, settings.rule_suggestions_path)

    init_db()

    # Load proposals
    proposals_data = store._load_json(store.proposals_path, {})
    if not proposals_data:
        print("⚠️ No proposals found.")
        return

    # Filter to pending
    if args.all:
        targets = [(sha, p) for sha, p in proposals_data.items() if p.get("status") == "pending"]
    elif args.sha:
        targets = [(sha, p) for sha, p in proposals_data.items() if sha in args.sha]
    else:
        print("⚠️ Specify --all or --sha SHA1 SHA2...")
        return

    if not targets:
        print("⚠️ No matching pending proposals.")
        return

    moved = 0
    skipped = 0
    errors = 0
    lines = []

    for sha256, proposal in targets:
        proposal_detail = proposal.get("proposal", {})
        override_name = getattr(args, "name", None) if len(targets) == 1 else None
        override_dest = getattr(args, "dest", None) if len(targets) == 1 else None

        if args.dry_run:
            name = override_name or proposal_detail.get("proposedName", "?")
            dest = override_dest or proposal_detail.get("proposedDest", "?")
            lines.append(f"  [DRY-RUN] {proposal.get('filename', '?')} → {name} in {dest}")
            continue

        result = approve_proposal(settings, sha256, override_dest=override_dest, override_name=override_name)

        if result.get("ok"):
            name = result.get("movedTo", "?")
            lines.append(f"  ✅ {proposal_detail.get('proposedName', '?')} → {name}")
            moved += 1

            # ── Save correction if classification was overridden ──
            correction_applied = False
            if override_name or override_dest:
                try:
                    from app.classify.corrections import save_correction
                    original_type = proposal_detail.get("docType", "Unknown")
                    original_person = proposal_detail.get("person", "Unknown")
                    original_provider = proposal_detail.get("provider", "Unknown")
                    original_confidence = proposal_detail.get("confidence", 0)
                    org_id = proposal_detail.get("ruleMatchId", "").split(":")[-1] if ":" in proposal_detail.get("ruleMatchId", "") else None
                    corrected_person = original_person
                    corrected_provider = original_provider
                    corrected_type = original_type
                    if override_name:
                        pass  # Name override doesn't change type
                    save_correction(
                        original_doc_type=original_type,
                        original_person=original_person,
                        original_provider=original_provider,
                        original_confidence=original_confidence,
                        corrected_doc_type=corrected_type,
                        corrected_person=corrected_person,
                        corrected_provider=corrected_provider,
                        org_id=org_id,
                        sha256=sha256,
                    )
                    correction_applied = True
                except Exception:
                    pass

            # ── Lifecycle tracking: update approval ──
            try:
                from app.state.scan_db import update_lifecycle_approval
                final_name = override_name or proposal_detail.get("proposedName", "")
                final_dest = override_dest or proposal_detail.get("proposedDest", "")
                final_type = proposal_detail.get("docType", "")
                final_person = proposal_detail.get("person", "")
                final_provider = proposal_detail.get("provider", "")
                override = "none" if not (override_name or override_dest) else "rename"
                update_lifecycle_approval(
                    sha256=sha256,
                    final_name=final_name,
                    final_dest=final_dest,
                    final_doc_type=final_type,
                    final_person=final_person,
                    final_provider=final_provider,
                    override_type=override,
                    correction_applied=correction_applied,
                )
            except Exception:
                pass

            # Update scan DB
            try:
                src = Path(proposal.get("path", ""))
                if src.exists():
                    update_result_status_by_path(str(src), "approved")
                dest_path = Path(result["movedTo"])
                log_file_move(str(proposal.get("path", "")), str(dest_path), success=True)
            except Exception:
                pass
        else:
            lines.append(f"  ❌ {proposal.get('filename', '?')}: {result.get('error', 'unknown')}")
            errors += 1

    print("\n".join(lines))
    print(f"\n📊 Moved: {moved} | Skipped: {skipped} | Errors: {errors}")


# ── Phase 3: Full workflow ────────────────────────────────────────

def cmd_run(args) -> None:
    """Full workflow: scan → propose → approve → move → cleanup."""
    results = cmd_scan(args)
    if not results:
        return

    if args.yes:
        # Auto-approve all
        print("\n✅ Auto-approving all proposals...")
        args.all = True
        args.sha = None
        args.dry_run = getattr(args, "dry_run", False)
        cmd_approve(args)
    else:
        # Interactive: show proposals and ask
        print("\n" + "=" * 60)
        print("📋 Review the proposals above. To approve, run:")
        print(f"   python3 scan_workflow.py approve --all")
        print(f"   python3 scan_workflow.py approve --sha SHA1 SHA2...")
        print("=" * 60)


# ── Phase 4: Watch daemon ─────────────────────────────────────────

def cmd_watch(args) -> None:
    """Filesystem watcher daemon — monitors inbox and auto-processes."""
    try:
        from watchdog.events import FileSystemEvent, FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        print("ERROR: watchdog is required. Install with: pip install watchdog")
        sys.exit(1)

    settings = load_settings()

    print(f"👁️  Watching inbox: {settings.inbox_root}")
    print(f"📁 QSync root: {settings.qsync_root}")
    print("Press Ctrl+C to stop.\n")

    # Process existing files first
    _watch_process(args)

    class InboxHandler(FileSystemEventHandler):
        def __init__(self, debounce_sec=5.0):
            self._debounce_sec = debounce_sec
            self._timer = None
            self._lock = threading.Lock()

        def _schedule(self):
            with self._lock:
                if self._timer:
                    self._timer.cancel()
                self._timer = threading.Timer(self._debounce_sec, _watch_process, args=[args])
                self._timer.daemon = True
                self._timer.start()

        def on_created(self, event):
            if not event.is_directory:
                self._schedule()

        def on_moved(self, event):
            if not event.is_directory:
                self._schedule()

    import threading
    handler = InboxHandler(debounce_sec=5.0)
    observer = Observer()
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


def _watch_process(args):
    """Process inbox files (called by watcher on file events)."""
    results = cmd_scan(args)
    if results and getattr(args, "yes", False):
        args.all = True
        args.sha = None
        args.dry_run = getattr(args, "dry_run", False)
        cmd_approve(args)


# ── Formatting ────────────────────────────────────────────────────

def format_proposals(results: list[dict]) -> str:
    """Format scan results as a readable summary."""
    if not results:
        return "📭 Inbox is empty."

    lines = [f"\n📋 **{len(results)} file(s) found in inbox:**\n"]
    lines.append(f"{'#':<3} {'Cache':<5} {'Original':<35} {'Proposed Name':<55} {'Destination':<45} {'Person':<10} {'Type':<16} {'Conf':>5}")
    lines.append("─" * 175)

    for i, r in enumerate(results, 1):
        conf = r.get("confidence", 0)
        conf_pct = f"{conf * 100:.0f}%" if conf <= 1 else f"{conf:.0f}%"
        cache_tag = "📦" if r.get("ocr_from_cache") else "🔍"
        lines.append(
            f"{i:<3} {cache_tag:<5} "
            f"{r.get('filename', '?')[:34]:<35} "
            f"{r.get('proposedName', '?')[:54]:<55} "
            f"{r.get('proposedDest', '?')[:44]:<45} "
            f"{r.get('person', '?')[:9]:<10} "
            f"{r.get('docType', '?')[:15]:<16} "
            f"{conf_pct:>5}"
        )

    # Cache summary line
    cached_count = sum(1 for r in results if r.get("ocr_from_cache"))
    fresh_count = len(results) - cached_count
    lines.append("")
    if cached_count:
        lines.append(f"📦 = from OCR cache | 🔍 = freshly OCR'd | {cached_count} cached, {fresh_count} fresh")
    else:
        lines.append(f"🔍 = freshly OCR'd | All {len(results)} results are new OCR runs")

    # Also output JSON for programmatic consumption
    lines.append("\n📦 JSON (for automation):")
    lines.append(json.dumps([{
        "id": r.get("id", i),
        "sha256": r.get("sha256", ""),
        "path": r.get("path", ""),
        "filename": r.get("filename", ""),
        "proposedName": r.get("proposedName", ""),
        "proposedDest": r.get("proposedDest", ""),
        "person": r.get("person", ""),
        "docType": r.get("docType", ""),
        "confidence": r.get("confidence", 0),
        "ocr_from_cache": r.get("ocr_from_cache", False),
    } for i, r in enumerate(results, 1)], indent=2))

    return "\n".join(lines)


# ── OCR Cache Commands ──────────────────────────────────────────────

def cmd_ocr_cache(args) -> None:
    """Handle ocr-cache subcommands."""
    cmd = getattr(args, 'ocr_cache_command', None)
    if not cmd:
        print("Usage: scan_workflow.py ocr-cache {show|list|rerun|clear}")
        return
    
    init_db()
    
    if cmd == "show":
        entry = None
        if args.sha:
            entry = get_ocr_cache(args.sha)
        elif args.path:
            entry = get_ocr_cache_by_path(args.path)
        else:
            print("⚠️ Specify --sha or --path")
            return
        
        if not entry:
            print("📭 No cached OCR found.")
            return
        
        text = entry["ocr_text"]
        max_chars = None if args.full else 500
        display = text[:max_chars] if max_chars else text
        
        print(f"📄 SHA256: {entry['sha256'][:16]}...")
        print(f"📁 Path: {entry['file_path']}")
        print(f"📄 Filename: {entry['filename']}")
        print(f"🗂️ Source: {entry['text_source']}")
        print(f"🕐 Cached: {entry['ocr_timestamp']}")
        if entry.get('ocr_duration_ms'):
            print(f"⏱️ Duration: {entry['ocr_duration_ms']}ms")
        if entry.get('file_size'):
            print(f"📦 Size: {entry['file_size']:,} bytes")
        print(f"📝 OCR text ({len(text)} chars){' (showing first 500)' if not args.full and len(text) > 500 else ''}:")
        print("─" * 60)
        print(display)
        if not args.full and len(text) > 500:
            print(f"\n... ({len(text) - 500} more chars. Use --full to see all.)")
        
        print(f"\n💡 Use `ocr-cache rerun --sha {entry['sha256'][:16]}...` to force re-OCR.")
    
    elif cmd == "list":
        entries = list_ocr_cache(limit=args.limit)
        if not entries:
            print("📭 OCR cache is empty.")
            return
        
        print(f"📋 OCR Cache ({len(entries)} entries, showing latest {args.limit}):")
        print(f"{'SHA256':<18} {'Filename':<40} {'Source':<12} {'Cached':<20} {'Chars':>6}")
        print("─" * 100)
        for e in entries:
            sha_short = e['sha256'][:16]
            print(f"{sha_short:<18} {e['filename'][:39]:<40} {e['text_source'][:11]:<12} {str(e['ocr_timestamp'])[:19]:<20} {e['ocr_text_len']:>6}")
    
    elif cmd == "rerun":
        settings = load_settings()
        rules = load_compiled_rules(settings.scan_rules_path)
        file_type_config = load_yaml_config(settings.file_types_path)
        
        # Find the file to re-OCR
        if args.sha:
            entry = get_ocr_cache(args.sha)
            if not entry:
                print(f"⚠️ No cache entry for SHA {args.sha}")
                return
            file_path = Path(entry["file_path"])
        elif args.path:
            entry = get_ocr_cache_by_path(args.path)
            if not entry:
                print(f"⚠️ No cache entry for path {args.path}")
                return
            file_path = Path(args.path)
        else:
            print("⚠️ Specify --sha or --path")
            return
        
        if not file_path.exists():
            print(f"⚠️ File not found: {file_path}")
            return
        
        print(f"🔄 Re-OCR'ing: {file_path.name}")
        result = scan_file(file_path, rules, file_type_config, force_ocr=True)
        if result and result.get("status") == "success":
            cached = result.get("ocr_from_cache", False)
            tag = "📦" if cached else "🔍"
            print(f"{tag} {file_path.name} → {result['proposedName']} (cache updated)")
            print(f"📝 OCR text preview: {result['ocrSample'][:200]}")
        else:
            print(f"❌ Re-OCR failed: {result}")
    
    elif cmd == "clear":
        if args.all:
            if not args.confirm:
                count = len(list_ocr_cache(limit=99999))
                print(f"⚠️ This will delete ALL {count} cache entries. Use --confirm to proceed.")
                return
            deleted = delete_ocr_cache(all_entries=True)
            print(f"🗑️ Cleared entire OCR cache ({deleted} entries deleted).")
        elif args.sha:
            deleted = delete_ocr_cache(sha256=args.sha)
            if deleted:
                print(f"🗑️ Deleted cache entry for SHA {args.sha[:16]}...")
            else:
                print(f"⚠️ No cache entry found for SHA {args.sha}")
        else:
            print("⚠️ Specify --sha or --all --confirm")
    
    else:
        print(f"Unknown ocr-cache command: {cmd}")


# ── Main ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scan workflow — single entry point for inbox scanning",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", help="Sub-command")

    # scan
    scan_parser = subparsers.add_parser("scan", help="Scan inbox and show proposals")
    scan_parser.add_argument("--force-ocr", action="store_true", help="Force re-OCR even if cache exists")

    # approve
    approve_parser = subparsers.add_parser("approve", help="Approve proposals and move files")
    approve_parser.add_argument("--all", action="store_true", help="Approve all pending proposals")
    approve_parser.add_argument("--sha", nargs="+", default=None, help="Approve specific SHA256 hashes")
    approve_parser.add_argument("--dest", default=None, help="Override destination (with single --sha)")
    approve_parser.add_argument("--name", default=None, help="Override filename (with single --sha)")
    approve_parser.add_argument("--dry-run", action="store_true", help="Preview without moving")

    # ocr-cache
    ocr_cache_parser = subparsers.add_parser("ocr-cache", help="View and manage OCR cache")
    ocr_cache_sub = ocr_cache_parser.add_subparsers(dest="ocr_cache_command", help="OCR cache sub-commands")
    
    # ocr-cache show
    show_parser = ocr_cache_sub.add_parser("show", help="Show cached OCR text for a file")
    show_parser.add_argument("--sha", help="SHA256 hash of the file")
    show_parser.add_argument("--path", help="File path to look up")
    show_parser.add_argument("--full", action="store_true", help="Show full OCR text (default: first 500 chars)")
    
    # ocr-cache list
    list_parser = ocr_cache_sub.add_parser("list", help="List all cached OCR entries")
    list_parser.add_argument("--limit", type=int, default=50, help="Max entries to show")
    
    # ocr-cache rerun
    rerun_parser = ocr_cache_sub.add_parser("rerun", help="Force re-OCR and update cache")
    rerun_parser.add_argument("--sha", help="SHA256 hash to re-OCR")
    rerun_parser.add_argument("--path", help="File path to re-OCR")
    
    # ocr-cache clear
    clear_parser = ocr_cache_sub.add_parser("clear", help="Clear OCR cache")
    clear_parser.add_argument("--sha", help="SHA256 hash to delete (omit to clear all)")
    clear_parser.add_argument("--all", action="store_true", help="Clear entire cache")
    clear_parser.add_argument("--confirm", action="store_true", help="Confirm clearing entire cache")

    # index — build/update the file duplicate index
    index_parser = subparsers.add_parser("index", help="Build/update the QSync file duplicate index")
    index_parser.add_argument("--force", "-f", action="store_true", help="Force full rebuild (ignore incremental)")

    # corrections — list classification corrections
    corrections_parser = subparsers.add_parser("corrections", help="List recent classification corrections")
    corrections_parser.add_argument("--limit", type=int, default=20, help="Max corrections to show")

    # correct — manually add a correction
    correct_parser = subparsers.add_parser("correct", help="Add a classification correction")
    correct_parser.add_argument("--sha", required=True, help="SHA256 hash of the file")
    correct_parser.add_argument("--type", required=True, help="Correct doc type (e.g., lab_requisition)")
    correct_parser.add_argument("--person", default=None, help="Correct person name")
    correct_parser.add_argument("--provider", default=None, help="Correct provider")

    # lifecycle — view scan lifecycle stats and history
    lifecycle_parser = subparsers.add_parser("lifecycle", help="View scan lifecycle stats and history")
    lifecycle_parser.add_argument("--sha", default=None, help="SHA256 hash to view full history")
    lifecycle_parser.add_argument("--stats", action="store_true", help="Show aggregate statistics")
    lifecycle_parser.add_argument("--limit", type=int, default=20, help="Max recent records to show")

    # run
    run_parser = subparsers.add_parser("run", help="Full workflow: scan → propose → approve")
    run_parser.add_argument("--yes", "-y", action="store_true", help="Auto-approve all (non-interactive)")
    run_parser.add_argument("--dry-run", action="store_true", help="Preview without moving")

    # watch
    watch_parser = subparsers.add_parser("watch", help="Filesystem watcher daemon")
    watch_parser.add_argument("--yes", "-y", action="store_true", help="Auto-approve all detected files")
    watch_parser.add_argument("--dry-run", action="store_true", help="Preview without moving")

    args = parser.parse_args()

    if not args.command:
        # Default: scan
        cmd_scan(args)
        return

    if args.command == "scan":
        cmd_scan(args)
    elif args.command == "approve":
        cmd_approve(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "watch":
        cmd_watch(args)
    elif args.command == "ocr-cache":
        cmd_ocr_cache(args)
    elif args.command == "index":
        cmd_index(args)
    elif args.command == "corrections":
        cmd_corrections(args)
    elif args.command == "correct":
        cmd_correct(args)
    elif args.command == "lifecycle":
        cmd_lifecycle(args)
    else:
        parser.print_help()


def cmd_lifecycle(args) -> None:
    """Show lifecycle stats or history for a specific file."""
    from app.state.scan_db import get_lifecycle_stats, get_lifecycle_history, get_recent_lifecycle
    if args.sha:
        history = get_lifecycle_history(args.sha)
        if not history:
            print(f"No lifecycle record found for SHA: {args.sha}")
            return
        lc = history["lifecycle"]
        print(f"📋 Lifecycle for {lc['original_filename']}")
        print(f"  SHA256:       {lc['sha256']}")
        print(f"  First seen:   {lc['first_seen_at']}")
        print(f"  OCR source:    {lc['text_source']} (quality: {lc['text_quality']:.2f})")
        print(f"  ── Proposed ──")
        print(f"  Name:         {lc['proposed_name']}")
        print(f"  Dest:         {lc['proposed_dest']}")
        print(f"  Type:         {lc['proposed_doc_type']}")
        print(f"  Person:        {lc['proposed_person']}")
        print(f"  Provider:     {lc['proposed_provider']}")
        print(f"  Confidence:    {lc['classification_confidence']:.2f}")
        print(f"  Rule:         {lc['rule_match_id']}")
        if lc['final_name']:
            print(f"  ── Final ──")
            print(f"  Name:         {lc['final_name']}")
            print(f"  Dest:         {lc['final_dest']}")
            print(f"  Type:         {lc['final_doc_type']}")
            print(f"  Person:       {lc['final_person']}")
            print(f"  Provider:     {lc['final_provider']}")
            print(f"  Override:     {lc['override_type']}")
            print(f"  Attempts:     {lc['approval_attempts']}")
            print(f"  Correction:    {'Yes' if lc['correction_applied'] else 'No'}")
            print(f"  Approved:     {lc['approved_at']}")
        else:
            print(f"  ── Status: Pending approval ──")
        if history["proposals"]:
            print(f"\n  📊 Proposal attempts ({len(history['proposals'])}):")
            for p in history["proposals"]:
                print(f"    #{p['attempt_number']} | {p['proposed_name'][:50]} | conf: {p['confidence']:.2f} | response: {p['response']}")
    elif args.stats:
        stats = get_lifecycle_stats()
        print("📊 Scan Lifecycle Statistics")
        print(f"  Total files tracked:     {stats['total_files']}")
        print(f"  Approved as proposed:    {stats['approved_as_proposed']}")
        print(f"  Overridden:             {stats['overridden']}")
        print(f"  Avg approval attempts:   {stats['avg_approval_attempts']}")
        if stats.get('override_breakdown'):
            print("\n  Override breakdown:")
            for otype, count in stats['override_breakdown'].items():
                print(f"    {otype}: {count}")
        if stats.get('doc_type_accuracy'):
            print("\n  Classification accuracy (by doc type):")
            for d in stats['doc_type_accuracy'][:8]:
                bar = "█" * int(d['accuracy'] * 10) + "░" * (10 - int(d['accuracy'] * 10))
                print(f"    {d['doc_type']:20s} {bar} {d['accuracy']:.0%} ({d['total']} files, {d['overridden']} overridden)")
        if stats.get('common_misclassifications'):
            print("\n  Top misclassifications:")
            for m in stats['common_misclassifications'][:5]:
                print(f"    {m['from']:20s} → {m['to']:20s} ({m['count']}x)")
    else:
        recent = get_recent_lifecycle(limit=args.limit)
        if not recent:
            print("No lifecycle records yet. Run a scan first.")
            return
        print(f"📋 Recent Scans (showing {len(recent)}):\n")
        for lc in recent:
            status = "✅" if lc['final_name'] else "⏳"
            override = f" ({lc['override_type']})" if lc['override_type'] != 'none' else ""
            attempts = f" [{lc['approval_attempts']} attempt{'s' if lc['approval_attempts'] != 1 else ''}]" if lc['approval_attempts'] > 0 else ""
            print(f"  {status} {lc['original_filename'][:40]:40s} → {lc['proposed_name'][:50] if lc['proposed_name'] else 'N/A':50s}{override}{attempts}")
            print(f"     type: {lc['proposed_doc_type']:15s} person: {lc['proposed_person']:10s} conf: {lc['classification_confidence']:.2f}")


def cmd_index(args) -> None:
    """Build or update the QSync file duplicate index."""
    from app.state.scan_db import build_file_index, init_db
    init_db()
    settings = load_settings()
    if args.force:
        # Clear existing index for full rebuild
        import sqlite3
        from app.state.scan_db import DB_PATH
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM file_index")
        conn.commit()
        conn.close()
        print("🗑️  Cleared existing index for full rebuild")
    print(f"📂 Building file index for {settings.qsync_root}...")
    stats = build_file_index(str(settings.qsync_root))
    print(f"✅ Index built: {stats['indexed']} indexed, {stats['skipped']} unchanged, {stats['errors']} errors")


def cmd_corrections(args) -> None:
    """List recent classification corrections."""
    from app.classify.corrections import list_corrections
    corrections = list_corrections(limit=args.limit)
    if not corrections:
        print("No corrections recorded yet.")
        return
    print(f"📋 Recent corrections (showing {len(corrections)}):\n")
    for c in corrections:
        print(f"  #{c['id']} | {c['original_doc_type']} → {c['corrected_doc_type']}")
        if c.get('org_id'):
            print(f"         org: {c['org_id']}")
        print(f"         person: {c['original_person']} → {c['corrected_person']}")
        print(f"         confidence: {c['original_confidence']:.2f} → {c.get('corrected_confidence', '?')}")
        print(f"         date: {c['correction_date']}")
        print()


def cmd_correct(args) -> None:
    """Manually add a classification correction."""
    from app.classify.corrections import save_correction
    correction = save_correction(
        original_doc_type="Unknown",
        original_person="Unknown",
        original_provider="Unknown",
        original_confidence=0.0,
        corrected_doc_type=args.type,
        corrected_person=args.person or "Unknown",
        corrected_provider=args.provider or "Unknown",
        org_id=None,
        sha256=args.sha,
    )
    print(f"✅ Correction saved: #{correction['id']}")
    print(f"   Type: {args.type}, Person: {args.person or 'Unknown'}, Provider: {args.provider or 'Unknown'}")


if __name__ == "__main__":
    main()