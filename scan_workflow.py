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

    # Auto-recover stuck files from processing/ (older than 30 min)
    processing_dir = settings.inbox_root.parent / "processing"
    if processing_dir.exists():
        recovered = 0
        for f in processing_dir.rglob("*"):
            if not f.is_file():
                continue
            import time
            age_min = (time.time() - f.stat().st_mtime) / 60
            if age_min > 30:
                dest = settings.inbox_root / f.name
                if not dest.exists():
                    try:
                        shutil.move(str(f), str(dest))
                        recovered += 1
                    except OSError:
                        pass
        if recovered:
            print(f"🔄 Auto-recovered {recovered} stuck file(s) from processing/")

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

    # Detect and split mixed-document page sets (e.g. FMLA + Surgery + Discharge in one scan stack)
    from app.backfill_reorganize import analyze_multi_document_set
    results = analyze_multi_document_set(results)

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

    # Log performance metrics
    try:
        from app.state.scan_db import log_metric
        avg_conf = sum(r.get("confidence", 0) for r in results) / len(results) if results else 0
        log_metric("batch_size", len(results), {"batch_id": batch_id})
        log_metric("batch_elapsed_sec", round(elapsed, 2), {"batch_id": batch_id, "worker_count": max_workers})
        log_metric("batch_cache_hits", cache_hits, {"batch_id": batch_id})
        log_metric("ocr_duration_avg_ms", round(sum(r.get("ocr_duration_ms", 0) or 0 for r in results) / len(results), 1), {"batch_id": batch_id}) if any(r.get("ocr_duration_ms") for r in results) else None
        log_metric("classification_confidence", round(avg_conf, 3), {"batch_id": batch_id})
    except Exception:
        pass

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






def cmd_recover(args) -> None:
    """Recover stuck files from processing/ directory.
    
    Also:
    1. Marks expired proposals (approved_at IS NULL, older than 24h) in scan_lifecycle
    2. Retries unrecovered failed moves from move_failed table
    3. Prints a summary
    
    # NOTE: The daily cron job should also call `scan_workflow.py recover`
    # to keep the pipeline clean. Add to OpenClaw cron config:
    #   openclaw cron add --name scan-recover --schedule '0 3 * * *' -- \
    #     python3 /home/ddieppa/.openclaw/workspace/scan-pipeline-v3/scan_workflow.py recover
    """
    from app.settings import load_settings
    settings = load_settings()
    
    processing = settings.state_dir.parent / "processing"
    inbox = settings.inbox_root
    
    if not processing.exists():
        processing = Path("/home/ddieppa/scanner/processing")
    
    if not inbox.exists():
        inbox = Path("/home/ddieppa/scanner/inbox")
    
    moved = 0
    skipped = 0
    
    # Move stuck files from processing/ back to inbox/
    if processing.exists():
        for f in processing.rglob("*"):
            if not f.is_file():
                continue
            # Check if file is old enough (older than 30 minutes)
            import time
            age_minutes = (time.time() - f.stat().st_mtime) / 60
            if age_minutes < 30 and not getattr(args, 'force', False):
                skipped += 1
                print(f"  ⏳ Too recent (< 30 min): {f.name} ({age_minutes:.0f} min old)")
                continue
            dest = inbox / f.name
            if dest.exists():
                print(f"  ⚠️ Already exists in inbox: {f.name}")
                continue
            try:
                shutil.move(str(f), str(dest))
                print(f"  ✅ Recovered: {f.name} → inbox/")
                moved += 1
            except OSError as e:
                print(f"  ❌ Failed to move {f.name}: {e}")
    
    # 0.4 — Expire stale proposals (approved_at IS NULL and older than 24h)
    expired_count = 0
    try:
        import sqlite3
        from app.state.scan_db import DB_PATH, init_db
        init_db()
        conn = sqlite3.connect(str(DB_PATH))
        c = conn.cursor()
        c.execute("""
            SELECT sha256, original_filename FROM scan_lifecycle
            WHERE approved_at IS NULL
              AND first_seen_at < datetime('now', '-24 hours')
        """)
        stale_rows = c.fetchall()
        if stale_rows:
            print(f"\n📋 Found {len(stale_rows)} expired proposal(s) (>24h without approval):")
            for sha, fname in stale_rows:
                c.execute("""
                    UPDATE scan_lifecycle
                    SET override_type = 'expired',
                        notes = COALESCE(notes, '') || 'Auto-expired after 24h without approval at ' || datetime('now')
                    WHERE sha256 = ? AND approved_at IS NULL
                """, (sha,))
                print(f"  ⏰ Expired: {fname} (first seen >24h ago)")
                expired_count += 1
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"  ⚠️ Could not check expired proposals: {e}")
    
    # 0.4 — Retry unrecovered failed moves
    from app.safe_move import list_failed_moves, recover_failed_move
    failed = list_failed_moves()
    recovered_moves = 0
    if failed:
        print(f"\n📋 Found {len(failed)} failed move(s) in database:")
        for f in failed:
            print(f"  ID {f['id']}: {Path(f['source_path']).name} → {f['target_path']}")
            print(f"     Reason: {f['reason']}")
            # Try to recover
            if Path(f['source_path']).exists():
                result = recover_failed_move(f['id'])
                if result['ok']:
                    print(f"     ✅ Recovered!")
                    recovered_moves += 1
                else:
                    print(f"     ❌ Still failing: {result['error']}")
            else:
                print(f"     ⚠️ Source file no longer exists")
    
    print(f"\n📊 Recovery summary: recovered {moved} files, expired {expired_count} proposals, retried {recovered_moves} moves")


def cmd_failed_moves(args) -> None:
    """List failed moves from the database."""
    from app.safe_move import list_failed_moves
    
    failed = list_failed_moves()
    if not failed:
        print("No failed moves.")
        return
    
    print(f"📋 {len(failed)} failed move(s):")
    print()
    from pathlib import Path
    for f in failed:
        source_name = Path(f['source_path']).name
        target_short = f['target_path'][-60:] if len(f['target_path']) > 60 else f['target_path']
        print(f"  ID {f['id']}: {source_name}")
        print(f"     → {target_short}")
        print(f"     Reason: {f['reason']}")
        print(f"     Failed: {f['failed_at']}")
        if f.get('recovered_at'):
            print(f"     Recovered: {f['recovered_at']} ({f.get('recovery_action', 'unknown')})")
        print()




def cmd_status(args) -> None:
    """3.1 — Show pipeline status dashboard."""
    import sqlite3
    import time
    from app.state.scan_db import DB_PATH, init_db
    from app.settings import load_settings
    
    settings = load_settings()
    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    
    print("📊 Scan Pipeline Status")
    print("═" * 50)
    
    # Watcher: running?
    watcher_running = False
    watcher_pid_file = Path("/home/ddieppa/scanner/.watcher.pid")
    if watcher_pid_file.exists():
        try:
            pid = int(watcher_pid_file.read_text().strip())
            import os
            os.kill(pid, 0)  # Check if process exists
            watcher_running = True
            print(f"  👁️  Watcher:       running (PID {pid})")
        except (ProcessLookupError, ValueError, PermissionError):
            print(f"  👁️  Watcher:       stopped (stale PID file)")
    else:
        # Try pgrep as fallback
        import subprocess
        try:
            result = subprocess.run(["pgrep", "-f", "watch-inbox.sh"], capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                watcher_running = True
                print(f"  👁️  Watcher:       running (pgrep found)")
            else:
                print(f"  👁️  Watcher:       stopped")
        except Exception:
            print(f"  👁️  Watcher:       unknown")
    
    # inbox/ file count + oldest
    inbox = settings.inbox_root
    if inbox.exists():
        inbox_files = list(inbox.rglob("*"))
        inbox_files = [f for f in inbox_files if f.is_file()]
        count = len(inbox_files)
        oldest_age = ""
        if inbox_files:
            oldest = min(f.stat().st_mtime for f in inbox_files)
            age_min = (time.time() - oldest) / 60
            if age_min < 60:
                oldest_age = f"{age_min:.0f}m"
            elif age_min < 1440:
                oldest_age = f"{age_min/60:.1f}h"
            else:
                oldest_age = f"{age_min/1440:.1f}d"
        print(f"  📥 inbox/:        {count} file(s){f' (oldest: {oldest_age})' if oldest_age else ''}")
    else:
        print(f"  📥 inbox/:        not found")
    
    # processing/ file count + oldest
    processing = settings.state_dir.parent / "processing"
    if not processing.exists():
        processing = Path("/home/ddieppa/scanner/processing")
    if processing.exists():
        proc_files = [f for f in processing.rglob("*") if f.is_file()]
        count = len(proc_files)
        suspicious = ""
        oldest_age = ""
        if proc_files:
            oldest = min(f.stat().st_mtime for f in proc_files)
            age_min = (time.time() - oldest) / 60
            if age_min < 60:
                oldest_age = f"{age_min:.0f}m"
            elif age_min < 1440:
                oldest_age = f"{age_min/60:.1f}h"
            else:
                oldest_age = f"{age_min/1440:.1f}d"
            if age_min > 30:
                suspicious = " ⚠️ STUCK?"
        print(f"  ⚙️  processing/:   {count} file(s){f' (oldest: {oldest_age})' if oldest_age else ''}{suspicious}")
    else:
        print(f"  ⚙️  processing/:   not found")
    
    # Pending proposals
    proposals_path = settings.state_dir / "proposals.json"
    pending_count = 0
    if proposals_path.exists():
        try:
            proposals = json.loads(proposals_path.read_text())
            pending_count = sum(1 for p in proposals.values() if p.get("status") == "pending")
        except Exception:
            pass
    print(f"  📋 Pending proposals: {pending_count}")
    
    # Failed moves
    c.execute("SELECT COUNT(*) FROM move_failed WHERE recovered_at IS NULL")
    failed_moves = c.fetchone()[0]
    print(f"  ❌ Failed moves:    {failed_moves}")
    
    # Failed notifications
    try:
        c.execute("SELECT COUNT(*) FROM notification_log WHERE success = 0")
        failed_notifs = c.fetchone()[0]
    except sqlite3.OperationalError:
        failed_notifs = 0  # Table may not exist
    print(f"  🔔 Failed notifications: {failed_notifs}")
    
    # Last scan
    last_batch_path = settings.state_dir / "last_batch.json"
    if last_batch_path.exists():
        try:
            lb = json.loads(last_batch_path.read_text())
            ts = lb.get("timestamp", "unknown")
            print(f"  🕐 Last scan:      {ts}")
        except Exception:
            print(f"  🕐 Last scan:      error reading")
    else:
        print(f"  🕐 Last scan:      never")
    
    # QSync mount check
    qsync = Path("/mnt/e/QSync")
    qsync_ok = qsync.exists() and qsync.is_dir()
    print(f"  💾 QSync mount:    {'✅ accessible' if qsync_ok else '❌ NOT accessible'}")
    
    # QSync disk space
    if qsync_ok:
        try:
            import shutil as _shutil
            usage = _shutil.disk_usage(str(qsync))
            free_mb = usage.free // (1024 * 1024)
            total_mb = usage.total // (1024 * 1024)
            pct = usage.used / usage.total * 100
            print(f"  💿 QSync disk:     {free_mb:,} MB free / {total_mb:,} MB total ({pct:.0f}% used)")
        except Exception:
            print(f"  💿 QSync disk:     unable to check")
    
    # DB size
    try:
        db_size_mb = DB_PATH.stat().st_size / (1024 * 1024)
        print(f"  🗃️  DB size:        {db_size_mb:.1f} MB")
    except Exception:
        print(f"  🗃️  DB size:       unknown")
    
    # OCR cache count
    c.execute("SELECT COUNT(*) FROM ocr_cache")
    ocr_count = c.fetchone()[0]
    print(f"  📦 OCR cache:     {ocr_count} entries")
    
    # File index count + sidecar counts
    c.execute("SELECT COUNT(*), SUM(sidecar_has_meta), SUM(sidecar_has_ocr) FROM file_index")
    row = c.fetchone()
    idx_count = row[0] or 0
    meta_count = row[1] or 0
    ocr_idx_count = row[2] or 0
    print(f"  🗂️  File index:    {idx_count} entries ({meta_count} with .meta.json, {ocr_idx_count} with .ocr.txt)")
    
    conn.close()


def cmd_trace(args) -> None:
    """3.3 — Trace a document through the entire pipeline."""
    import sqlite3
    from app.state.scan_db import DB_PATH, init_db
    
    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    query = args.query.strip()
    
    # Find the lifecycle record — by SHA256 or partial filename
    if len(query) == 64 and all(ch in '0123456789abcdef' for ch in query.lower()):
        # Exact SHA256
        c.execute("SELECT * FROM scan_lifecycle WHERE sha256 = ?", (query.lower(),))
    else:
        # Partial filename match
        c.execute("SELECT * FROM scan_lifecycle WHERE original_filename LIKE ? ORDER BY first_seen_at DESC LIMIT 1",
                  (f"%{query}%",))
    
    row = c.fetchone()
    if not row:
        print(f"❌ No document found matching: {query}")
        conn.close()
        return
    
    lc = dict(row)
    sha = lc['sha256']
    
    # Header
    final_name = lc.get('final_name') or lc.get('proposed_name') or lc['original_filename']
    print(f"📄 {final_name}")
    print()
    
    # 1. DETECTED
    first_seen = lc.get('first_seen_at', '')
    original_path = lc.get('original_path', '')
    original_name = lc.get('original_filename', '')
    if first_seen:
        print(f"{first_seen}  DETECTED    {original_path or original_name}")
    
    # 2. OCR
    text_source = lc.get('text_source', '')
    text_quality = lc.get('text_quality', 0)
    if text_source:
        quality_str = f"{text_quality:.2f}" if text_quality else "N/A"
        print(f"          OCR         {text_source} (quality: {quality_str})")
    
    # 3. CLASSIFIED
    proposed_type = lc.get('proposed_doc_type', '')
    cls_conf = lc.get('classification_confidence', 0)
    if proposed_type:
        print(f"          CLASSIFIED   {proposed_type} (confidence: {cls_conf:.2f})")
    
    # 4. PROPOSED
    proposed_name = lc.get('proposed_name', '')
    if proposed_name:
        print(f"          PROPOSED    {proposed_name}")
    
    # 5. Proposals history
    c.execute("SELECT * FROM scan_proposals WHERE sha256 = ? ORDER BY attempt_number", (sha,))
    proposals = [dict(r) for r in c.fetchall()]
    for p in proposals:
        p_time = p.get('proposed_at', '')
        resp = p.get('response', 'pending')
        if resp != 'pending' or p.get('attempt_number', 1) > 1:
            print(f"{str(p_time)[:19] if p_time else ''}  PROPOSAL #{p.get('attempt_number', '?')}   response: {resp}")
    
    # 6. APPROVED
    approved_at = lc.get('approved_at')
    override = lc.get('override_type', 'none')
    if approved_at:
        override_str = f"override: {override}" if override != 'none' else 'as proposed'
        print(f"{str(approved_at)[:19]}  APPROVED    {override_str}")
    elif override == 'expired':
        notes = lc.get('notes', '')
        print(f"  ⏰ EXPIRED    {notes[:60]}" if notes else "  ⏰ EXPIRED")
    elif override == 'deny':
        reason = lc.get('rejection_reason', '')
        print(f"  ❌ DENIED     {reason}")
    else:
        print(f"  ⏳ PENDING    awaiting approval")
    
    # 7. MOVED — check file_moves table
    c.execute("""SELECT fm.* FROM file_moves fm
        JOIN scan_results sr ON fm.result_id = sr.id
        WHERE sr.file_path = ?
        ORDER BY fm.move_date DESC LIMIT 5""", (lc.get('original_path', ''),))
    moves = [dict(r) for r in c.fetchall()]
    
    # Also check move_failed
    c.execute("SELECT * FROM move_failed WHERE source_path = ? ORDER BY failed_at DESC LIMIT 5",
              (lc.get('original_path', ''),))
    failed = [dict(r) for r in c.fetchall()]
    
    for m in moves:
        if m.get('success'):
            print(f"{str(m['move_date'])[:19]}  MOVED       {m['destination_path']}")
        else:
            print(f"{str(m['move_date'])[:19]}  MOVE FAILED {m.get('error_message', 'unknown error')}")
    
    for f in failed:
        if f.get('recovered_at'):
            print(f"{str(f['recovered_at'])[:19]}  RECOVERED   move retried successfully")
        else:
            print(f"{str(f['failed_at'])[:19]}  MOVE FAILED {f.get('reason', 'unknown')}")
    
    # 8. SIDECAR files
    if lc.get('final_name'):
        final_dest = lc.get('final_dest', '')
        if final_dest:
            dest_path = Path(final_dest)
            stem = dest_path.stem
            parent = dest_path.parent
            sidecars = []
            for suffix in ['.meta.json', '.ocr.txt']:
                sc = parent / f"{stem}{suffix}"
                if sc.exists():
                    sidecars.append(sc.name)
            if sidecars:
                print(f"          SIDECAR     {', '.join(sidecars)} created")
    
    conn.close()


def cmd_backfill(args) -> None:
    """Run the backfill/re-organize workflow on an existing QSync directory."""
    from app.backfill_reorganize import run_backfill
    settings = load_settings()
    run_backfill(
        target_dir=args.dir,
        settings=settings,
        fix_sidecars_only=args.fix_sidecars_only,
        skip_valid=args.skip_valid,
        dry_run=args.dry_run,
        interactive=args.interactive,
        max_workers=args.max_workers,
    )


def main():
    # ── Setup structured logging ──
    from app.settings import load_settings
    _settings = load_settings()
    from app.logging_config import setup_logging
    setup_logging(_settings.state_dir)

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

    # logs — view structured pipeline logs
    logs_parser = subparsers.add_parser("logs", help="View structured pipeline logs")
    logs_parser.add_argument("--sha", default=None, help="Filter by SHA256 hash")
    logs_parser.add_argument("--tail", type=int, default=20, help="Show last N entries (default 20)")
    logs_parser.add_argument("--level", default=None, help="Filter by level (ERROR, WARNING, INFO, DEBUG)")

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

    # notifications — list failed notification log entries
    notifications_parser = subparsers.add_parser("notifications", help="List failed notification log entries")
    notifications_parser.add_argument("--limit", type=int, default=20, help="Max entries to show")
    notifications_parser.add_argument("--all", action="store_true", help="Show all entries (not just failed)")

    # lifecycle — view scan lifecycle stats and history
    lifecycle_parser = subparsers.add_parser("lifecycle", help="View scan lifecycle stats and history")
    lifecycle_parser.add_argument("--sha", default=None, help="SHA256 hash to view full history")
    lifecycle_parser.add_argument("--stats", action="store_true", help="Show aggregate statistics")
    lifecycle_parser.add_argument("--days", type=int, default=30, help="Number of days for date filtering (default: 30)")
    lifecycle_parser.add_argument("--verbose", action="store_true", help="Show full confusion matrix and all details")
    lifecycle_parser.add_argument("--limit", type=int, default=20, help="Max recent records to show")

    # recover — move stuck files from processing/ back to inbox/
    recover_parser = subparsers.add_parser("recover", help="Recover stuck files from processing/")
    recover_parser.add_argument("--force", action="store_true", help="Move files regardless of age (default: 30 min minimum)")

    # failed-moves — list failed moves from database
    failed_parser = subparsers.add_parser("failed-moves", help="List failed file moves")

    # corrections review — review and promote/reject correction suggestions
    corrections_review_parser = subparsers.add_parser("corrections-review", help="Review pending correction suggestions")
    corrections_review_parser.add_argument("--accept", type=int, default=None, help="Accept and promote suggestion by ID")
    corrections_review_parser.add_argument("--reject", type=int, default=None, help="Reject suggestion by ID")

    # 3.1 — status dashboard command
    status_parser = subparsers.add_parser("status", help="Show pipeline status dashboard")

    # 3.3 — trace command
    trace_parser = subparsers.add_parser("trace", help="Trace a document through the pipeline")
    trace_parser.add_argument("query", help="Filename or SHA256 hash")

    # run
    run_parser = subparsers.add_parser("run", help="Full workflow: scan → propose → approve")
    run_parser.add_argument("--yes", "-y", action="store_true", help="Auto-approve all (non-interactive)")
    run_parser.add_argument("--dry-run", action="store_true", help="Preview without moving")

    # watch
    watch_parser = subparsers.add_parser("watch", help="Filesystem watcher daemon")
    watch_parser.add_argument("--yes", "-y", action="store_true", help="Auto-approve all detected files")
    watch_parser.add_argument("--dry-run", action="store_true", help="Preview without moving")

    # backfill — re-process and re-organize already-filed documents
    backfill_parser = subparsers.add_parser("backfill", help="Re-process and re-organize already-filed documents")
    backfill_parser.add_argument("--dir", required=True, help="Directory to scan for documents needing backfill")
    backfill_parser.add_argument("--fix-sidecars-only", action="store_true", help="Only fix missing/empty sidecars, don't move files")
    backfill_parser.add_argument("--skip-valid", action="store_true", help="Skip files that already have valid sidecars")
    backfill_parser.add_argument("--dry-run", action="store_true", help="Show proposals without moving")
    backfill_parser.add_argument("--interactive", "-i", action="store_true", help="Ask for approval on each file")
    backfill_parser.add_argument("--max-workers", type=int, default=4, help="Parallel workers for OCR")

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
    elif args.command == "notifications":
        cmd_notifications(args)
    elif args.command == "logs":
        cmd_logs(args)
    elif args.command == "lifecycle":
        cmd_lifecycle(args)
    elif args.command == "recover":
        cmd_recover(args)
    elif args.command == "failed-moves":
        cmd_failed_moves(args)
    elif args.command == "corrections-review":
        cmd_corrections_review(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "trace":
        cmd_trace(args)
    elif args.command == "backfill":
        cmd_backfill(args)
    else:
        parser.print_help()



def cmd_corrections_review(args) -> None:
    """Review, accept, or reject pending correction suggestions.

    Reads rule_suggestions.yaml and classification_corrections table.
    With --accept ID: promotes a suggestion into scan_rules.yaml.
    With --reject ID: marks a suggestion as rejected.
    """
    import sqlite3
    from app.state.scan_db import DB_PATH, init_db
    init_db()

    settings = load_settings()
    suggestions_path = settings.rule_suggestions_path
    rules_path = settings.scan_rules_path

    # Handle --accept: promote suggestion into scan_rules.yaml
    accept_id = getattr(args, 'accept', None)
    if accept_id is not None:
        _promote_suggestion(accept_id, suggestions_path, rules_path)
        return

    # Handle --reject: mark suggestion as rejected
    reject_id = getattr(args, 'reject', None)
    if reject_id is not None:
        _reject_suggestion(reject_id, suggestions_path)
        return

    # Default: show pending suggestions from both sources
    print("📋 Pending Correction Suggestions:\n")

    # 1. From rule_suggestions.yaml
    suggestions = []
    if suggestions_path.exists():
        try:
            import yaml
            data = yaml.safe_load(suggestions_path.read_text(encoding="utf-8")) or {}
            suggestions = data.get("suggestions", [])
        except Exception:
            pass

    if suggestions:
        for i, s in enumerate(suggestions, 1):
            if s.get("status") == "rejected":
                continue
            hits = s.get("hit_count", s.get("hits", 0))
            desc = s.get("description", s.get("suggestion", "?"))
            last_seen = s.get("last_seen", s.get("generated_at", "?"))
            print(f"  #{i} ({hits} hits): {desc}")
            print(f"     Last seen: {last_seen}")
            print(f"     Accept: scan_workflow.py corrections-review --accept {i}")
            print(f"     Reject: scan_workflow.py corrections-review --reject {i}")
            print()

    # 2. From classification_corrections table (recent corrections)
    rows = []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
            SELECT id, original_doc_type, corrected_doc_type, original_person, corrected_person,
                   original_provider, corrected_provider, org_id, correction_date,
                   sample_keywords
            FROM classification_corrections
            ORDER BY correction_date DESC LIMIT 20
        """)
        rows = c.fetchall()
        conn.close()

        if rows:
            print("  Recent classification corrections:\n")
            for r in rows:
                rd = dict(r)
                print(f"  ID {rd['id']}: {rd['original_doc_type']} → {rd['corrected_doc_type']}")
                if rd.get('original_person') and rd['original_person'] != 'Unknown':
                    print(f"         person: {rd['original_person']} → {rd['corrected_person']}")
                if rd.get('org_id'):
                    print(f"         org: {rd['org_id']}")
                print(f"         date: {rd['correction_date']}")
                print()
    except Exception as e:
        print(f"  ⚠️ Could not read corrections table: {e}")

    if not suggestions and not rows:
        print("  No pending suggestions or corrections found.")


def _promote_suggestion(suggestion_id: int, suggestions_path: Path, rules_path: Path) -> None:
    """Promote a rule suggestion into scan_rules.yaml."""
    import yaml

    if not suggestions_path.exists():
        print(f"⚠️ No suggestions file found at {suggestions_path}")
        return

    data = yaml.safe_load(suggestions_path.read_text(encoding="utf-8")) or {}
    suggestions = data.get("suggestions", [])

    if suggestion_id < 1 or suggestion_id > len(suggestions):
        print(f"⚠️ Invalid suggestion ID: {suggestion_id}. Valid range: 1-{len(suggestions)}")
        return

    s = suggestions[suggestion_id - 1]
    if s.get("status") == "rejected":
        print(f"⚠️ Suggestion #{suggestion_id} was already rejected.")
        return

    # Load current scan_rules.yaml
    if not rules_path.exists():
        print(f"⚠️ Rules file not found at {rules_path}")
        return

    rules_data = yaml.safe_load(rules_path.read_text(encoding="utf-8"))

    # Add the suggestion as a new pattern under the appropriate doc type
    doc_type = s.get("doc_type", s.get("corrected_doc_type", "")).lower()
    pattern = s.get("pattern", s.get("keyword", ""))

    # Find the document type in rules and append the pattern
    promoted = False
    doc_types = rules_data.get("document_types", [])
    for dt in doc_types:
        if dt.get("id") == doc_type:
            patterns = dt.get("patterns", [])
            comment = f"# promoted from correction ID {suggestion_id}"
            patterns.append(f"{pattern}  {comment}" if pattern else comment)
            dt["patterns"] = patterns
            promoted = True
            break

    if not promoted:
        print(f"⚠️ Could not find doc_type '{doc_type}' in scan_rules.yaml")
        return

    # Save updated rules
    rules_path.write_text(yaml.dump(rules_data, default_flow_style=False, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # Mark suggestion as promoted
    s["status"] = "promoted"
    s["promoted_at"] = datetime.now().isoformat()
    suggestions_path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False), encoding="utf-8")

    print(f"✅ Suggestion #{suggestion_id} promoted to scan_rules.yaml")
    print(f"   Added pattern '{pattern}' under doc_type '{doc_type}'")


def _reject_suggestion(suggestion_id: int, suggestions_path: Path) -> None:
    """Mark a rule suggestion as rejected."""
    import yaml

    if not suggestions_path.exists():
        print(f"⚠️ No suggestions file found at {suggestions_path}")
        return

    data = yaml.safe_load(suggestions_path.read_text(encoding="utf-8")) or {}
    suggestions = data.get("suggestions", [])

    if suggestion_id < 1 or suggestion_id > len(suggestions):
        print(f"⚠️ Invalid suggestion ID: {suggestion_id}. Valid range: 1-{len(suggestions)}")
        return

    s = suggestions[suggestion_id - 1]
    s["status"] = "rejected"
    s["rejected_at"] = datetime.now().isoformat()
    suggestions_path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False), encoding="utf-8")

    print(f"❌ Suggestion #{suggestion_id} rejected.")


def cmd_logs(args) -> None:
    """View structured pipeline logs with optional filtering."""
    import json as _json
    settings = load_settings()
    log_dir = settings.state_dir / "logs"
    log_file = log_dir / "pipeline.log"
    if not log_file.exists():
        print(f"No log file found at {log_file}")
        return

    try:
        lines = log_file.read_text(encoding="utf-8").strip().split("\n")
    except OSError as e:
        print(f"Error reading log: {e}")
        return

    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = _json.loads(line)
        except _json.JSONDecodeError:
            continue

        if args.sha and entry.get("sha256", "") != args.sha:
            continue
        if args.level and entry.get("level", "") != args.level.upper():
            continue
        entries.append(entry)

    entries = entries[-args.tail:]

    if not entries:
        print("No matching log entries found.")
        return

    for e in entries:
        ts = e.get("ts", "?")
        level = e.get("level", "?")
        module = e.get("module", "?")
        msg = e.get("msg", "?")
        sha = e.get("sha256", "")
        phase = e.get("phase", "")
        duration = e.get("duration_ms", "")
        extra = []
        if sha:
            extra.append(f"sha={sha[:16]}...")
        if phase:
            extra.append(f"phase={phase}")
        if duration is not None:
            extra.append(f"duration={duration}ms")
        extra_str = f" [{', '.join(extra)}]" if extra else ""
        print(f"{ts} {level} {module}: {msg}{extra_str}")


def cmd_notifications(args) -> None:
    """List notification log entries (failed by default, or all with --all)."""
    from app.state.scan_db import list_failed_notifications, init_db
    init_db()
    if getattr(args, 'all', False):
        # Show all notifications
        import sqlite3
        from app.state.scan_db import DB_PATH
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('SELECT * FROM notification_log ORDER BY created_at DESC LIMIT ?', (args.limit,))
        rows = [dict(row) for row in c.fetchall()]
        conn.close()
        label = "notification"
    else:
        rows = list_failed_notifications(limit=args.limit)
        label = "failed notification"

    if not rows:
        print(f"No {label}s found.")
        return

    print(f"📋 {len(rows)} {label}(s):")
    for r in rows:
        status_icon = {"sent": "✅", "failed": "❌", "retried": "🔄"}.get(r.get('status', ''), "?")
        print(f"  {status_icon} [{r.get('status', '?')}] attempt {r.get('attempt', '?')} | {r.get('channel', '?')} → {r.get('target', '?')[:60]}")
        if r.get('error'):
            print(f"     Error: {r['error'][:100]}")
        print(f"     SHA/Batch: {r.get('sha256', '?')[:20]} | {r.get('created_at', '?')}")


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
        # 2.2 — Extended stats with date filtering
        import sqlite3
        from app.state.scan_db import DB_PATH, init_db
        init_db()
        days = getattr(args, 'days', 30)
        verbose = getattr(args, 'verbose', False)
        
        stats = get_lifecycle_stats(days=days)
        print(f"📊 Scan Lifecycle Statistics (last {days} days)")
        print(f"  Total files tracked:     {stats['total_files']}")
        print(f"  Approved as proposed:    {stats['approved_as_proposed']}")
        print(f"  Overridden:             {stats['overridden']}")
        print(f"  Avg approval attempts:   {stats['avg_approval_attempts']}")
        
        # Auto-approved vs manual vs override vs denied
        if stats.get('approval_breakdown'):
            print("\n  Approval breakdown:")
            for atype, count in stats['approval_breakdown'].items():
                pct = (count / stats['total_files'] * 100) if stats['total_files'] else 0
                print(f"    {atype:20s} {count:4d} ({pct:.0f}%)")
        
        if stats.get('override_breakdown'):
            print("\n  Override breakdown:")
            for otype, count in stats['override_breakdown'].items():
                print(f"    {otype}: {count}")
        
        # Per-doc-type classification accuracy
        if stats.get('doc_type_accuracy'):
            print("\n  Classification accuracy (by doc type):")
            for d in stats['doc_type_accuracy'][:8]:
                bar = "█" * int(d['accuracy'] * 10) + "░" * (10 - int(d['accuracy'] * 10))
                print(f"    {d['doc_type']:20s} {bar} {d['accuracy']:.0%} ({d['total']} files, {d['overridden']} overridden)")
        
        # Average classification_confidence by doc_type
        if stats.get('confidence_by_type'):
            print("\n  Avg classification confidence (by doc type):")
            for d in stats['confidence_by_type'][:10]:
                bar_len = int(d['avg_confidence'] * 20)
                bar = "█" * bar_len + "░" * (20 - bar_len)
                print(f"    {d['doc_type']:20s} {bar} {d['avg_confidence']:.2f} ({d['total']} files)")
        
        # Confusion matrix: proposed vs final doc type
        if stats.get('confusion_matrix'):
            print("\n  Top misclassifications (proposed → final):")
            limit = None if verbose else 5
            for m in (stats['confusion_matrix'] if verbose else stats['confusion_matrix'][:5]):
                print(f"    {m['proposed']:20s} → {m['final']:20s} ({m['count']}x)")
        
        # Dead rules: patterns in scan_rules.yaml that have never matched
        if stats.get('dead_rules'):
            print("\n  ⚠️ Rules that have never matched (consider removing):")
            for r in stats['dead_rules'][:10]:
                print(f"    {r['rule_id']:30s} ({r['names'][:50]})")
        elif verbose:
            print("\n  ✅ All rules have matched at least once.")
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
