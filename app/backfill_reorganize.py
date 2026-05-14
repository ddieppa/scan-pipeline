"""Backfill/Re-organize — re-process already-filed documents.

Scans a QSync directory, detects missing/empty sidecars, re-OCRs, re-classifies,
detects multi-document page sets, and proposes renames + re-organizations.

Usage (via scan_workflow.py backfill):
    # Scan a directory, report issues, propose fixes
    python3 scan_workflow.py backfill --dir /mnt/e/QSync/02-Areas/Family/Natalie/Health/Providers/DiazSocarrasOBGYN

    # Fix only missing/empty sidecars (OCR + meta.json)
    python3 scan_workflow.py backfill --dir <path> --fix-sidecars-only

    # Dry-run: show proposals without moving
    python3 scan_workflow.py backfill --dir <path> --dry-run

    # Interactive: ask per-file
    python3 scan_workflow.py backfill --dir <path> --interactive

    # Skip files that already have valid sidecars (only process broken ones)
    python3 scan_workflow.py backfill --dir <path> --skip-valid
"""
from __future__ import annotations

import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from app.classify.config import load_compiled_rules, load_yaml_config
from app.classify.engine import ClassificationResult, classify_document, extract_page_indicator, group_sequential_pages
from app.extractors.docx import extract_docx
from app.extractors.images import extract_image
from app.extractors.pdf import extract_pdf
from app.extractors.xlsx import extract_xlsx
from app.extractors.common import ExtractionResult
from app.index_manager import update_health_index
from app.settings import Settings, load_settings
from app.sidecar import create_sidecars
from app.state.scan_db import (
    DB_PATH, init_db, get_ocr_cache, save_ocr_cache, delete_ocr_cache,
    get_ocr_cache_by_path, check_duplicate_index, update_lifecycle_approval,
    log_file_move,
)
from app.utils import normalize_spaces, safe_filename_component, scan_date_from_mtime, sha256_file


SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".pdf", ".docx", ".xlsx"}


def collect_target_files(target_dir: Path, recursive: bool = True) -> list[Path]:
    """Collect all document files in the target directory."""
    files = []
    globber = target_dir.rglob("*") if recursive else target_dir.iterdir()
    for f in globber:
        if not f.is_file():
            continue
        if f.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(f)
    return sorted(files)


def has_valid_sidecars(file_path: Path) -> tuple[bool, bool, str | None]:
    """Check if a file has valid .ocr.txt and .meta.json sidecars.

    Returns: (has_meta, has_valid_ocr, ocr_text_or_none)
    """
    stem = file_path.stem
    parent = file_path.parent
    meta_path = parent / f"{stem}.meta.json"
    ocr_path = parent / f"{stem}.ocr.txt"

    has_meta = meta_path.exists() and meta_path.stat().st_size > 10

    ocr_text = None
    has_valid_ocr = False
    if ocr_path.exists():
        try:
            text = ocr_path.read_text(encoding="utf-8", errors="replace")
            if len(text.strip()) > 20:
                has_valid_ocr = True
                ocr_text = text
        except Exception:
            pass

    return has_meta, has_valid_ocr, ocr_text


def extract_text(file_path: Path, file_type_config: dict, force_ocr: bool = False) -> tuple[ExtractionResult, int | None, bool]:
    """Extract text from a file. Returns (extraction, duration_ms, from_cache)."""
    sha = sha256_file(file_path)
    file_size = file_path.stat().st_size

    # Check OCR cache
    if not force_ocr:
        cached = get_ocr_cache(sha)
        if cached and cached.get("ocr_text") and len(cached["ocr_text"].strip()) > 20:
            return (
                ExtractionResult(
                    text=cached["ocr_text"],
                    text_source=cached.get("text_source", "ocr_cache"),
                    pages_inspected=0,
                    needs_ocr=False,
                    metadata={},
                ),
                None,
                True,
            )

    import time
    t0 = time.time()
    ext = file_path.suffix.lower()

    try:
        if ext == ".pdf":
            extraction = extract_pdf(
                file_path,
                inspect_pages=int(file_type_config.get("pdf", {}).get("inspect_pages", 10)),
                min_text_chars_before_skip_ocr=int(file_type_config.get("pdf", {}).get("min_text_chars_before_skip_ocr", 20)),
                render_dpi=int(file_type_config.get("pdf", {}).get("render_dpi", 200)),
            )
        elif ext in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}:
            extraction = extract_image(file_path, int(file_type_config.get("images", {}).get("max_ocr_chars", 20000)))
        elif ext == ".docx":
            extraction = extract_docx(file_path, 400, 200)
        elif ext == ".xlsx":
            extraction = extract_xlsx(file_path, 8, 300)
        else:
            raise RuntimeError(f"Unsupported: {ext}")
    except Exception as exc:
        raise RuntimeError(f"Extraction failed: {exc}") from exc

    duration_ms = int((time.time() - t0) * 1000)

    # Save to cache
    save_ocr_cache(sha, str(file_path), file_path.name, extraction.text,
                   extraction.text_source, duration_ms, file_size)

    return extraction, duration_ms, False


def classify_and_propose(file_path: Path, extraction: ExtractionResult, rules, scan_date: str) -> dict[str, Any]:
    """Classify a document and build a proposal."""
    classification = classify_document(extraction.text, file_path, scan_date, rules)

    return {
        "status": "success",
        "sha256": sha256_file(file_path),
        "path": str(file_path),
        "filename": file_path.name,
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
        "page_indicator": extract_page_indicator(extraction.text),
    }


def analyze_multi_document_set(results: list[dict]) -> list[dict]:
    """Analyze a batch of results from the same folder to detect multi-document scans.

    Returns re-grouped results with adjusted names that reflect the actual
    document types found on each page (like we did manually for the ectopic pregnancy).
    """
    # Group by (folder, total_pages) where page indicators exist
    from collections import defaultdict

    # First, apply the pipeline's normal sequential grouping
    results = group_sequential_pages(results)

    # Now detect page-to-page doc_type changes within a group
    # Build page groups: files that share the same parent folder
    parent_groups = defaultdict(list)
    for i, r in enumerate(results):
        if r.get("status") == "success":
            parent = str(Path(r["path"]).parent)
            parent_groups[parent].append((i, r))

    out = [dict(r) for r in results]

    for parent, members in parent_groups.items():
        if len(members) < 2:
            continue

        # Sort by page number if available
        members_sorted = sorted(members, key=lambda x: (x[1].get("page_indicator") or (999, 0))[0])

        # Detect document boundaries: when doc_type or provider changes significantly
        current_doc_type = None
        current_provider = None
        doc_boundary = 0
        doc_segments = []

        for idx, (orig_idx, r) in enumerate(members_sorted):
            dt = r.get("docType", "").lower()
            prov = r.get("provider", "").lower()

            # Detect boundary conditions
            is_boundary = False
            if current_doc_type and dt and dt != current_doc_type:
                # Major doc type change
                if not _is_related_type(current_doc_type, dt):
                    is_boundary = True

            if is_boundary:
                doc_segments.append((doc_boundary, idx))
                doc_boundary = idx

            current_doc_type = dt
            current_provider = prov

        doc_segments.append((doc_boundary, len(members_sorted)))

        # For each segment, re-propose a name reflecting the actual doc_type
        for seg_start, seg_end in doc_segments:
            segment = members_sorted[seg_start:seg_end]
            if not segment:
                continue

            # Pick representative doc_type and provider from this segment
            doc_types = [r.get("docType", "") for _, r in segment if r.get("docType")]
            providers = [r.get("provider", "") for _, r in segment if r.get("provider")]

            rep_doc_type = _most_common(doc_types) or "Document"
            rep_provider = _most_common(providers) or "Unknown"

            # Get date from first page
            scan_date = segment[0][1].get("scanDate", "")

            # Build a new proposed name for this segment
            for seg_idx, (orig_idx, r) in enumerate(segment):
                person = r.get("person", "Unknown")
                safe_provider = safe_filename_component(rep_provider)
                safe_type = safe_filename_component(rep_doc_type)
                safe_person = safe_filename_component(person)

                # Determine file extension
                orig_name = Path(r["path"]).name
                _, ext = orig_name.rsplit(".", 1) if "." in orig_name else (orig_name, "jpg")

                # Build name: date_provider_type_person_001.ext
                date_part = scan_date or "0000-00-00"
                name = f"{date_part}_{safe_provider}_{safe_type}_{safe_person}_{seg_idx:03d}.{ext}"

                # Override the proposed name
                out[orig_idx]["proposedName"] = name
                out[orig_idx]["segmentDocType"] = rep_doc_type
                out[orig_idx]["segmentProvider"] = rep_provider

                # The destination should also change if the doc_type implies a different subfolder
                # e.g., Hospitalization instead of Providers
                out[orig_idx]["proposedDest"] = _dest_for_doc_type(rep_doc_type, person, r.get("proposedDest", ""))

    return out


def _is_related_type(type_a: str, type_b: str) -> bool:
    """Check if two doc types are related (part of the same logical document)."""
    a = type_a.lower()
    b = type_b.lower()

    # Same exact type
    if a == b:
        return True

    # Related groupings
    related_groups = [
        {"dischargesummary", "hospitalrecord", "emergencycare", "errecord"},
        {"labresults", "labrequisition", "labreport"},
        {"prescription", "rx", "medication"},
        {"ultrasound", "imaging", "xray", "mri", "ct"},
        {"fmla", "workexcuse", "disability"},
        {"surgicalrecord", "operativereport", "procedurerecord"},
    ]

    for group in related_groups:
        if a in group and b in group:
            return True

    return False


def _most_common(items: list[str]) -> str | None:
    """Return the most common non-empty item."""
    from collections import Counter
    filtered = [i.strip() for i in items if i and i.strip()]
    if not filtered:
        return None
    return Counter(filtered).most_common(1)[0][0]


def _dest_for_doc_type(doc_type: str, person: str, current_dest: str) -> str:
    """Map a document type to its canonical PARA destination."""
    dt = doc_type.lower()

    # Hospitalization group
    if dt in {"dischargesummary", "hospitalrecord", "emergencycare", "errecord", "surgicalrecord", "operativereport"}:
        return f"02-Areas/Family/{person}/Health/Hospitalization/"

    # Lab group
    if dt in {"labresults", "labrequisition", "labreport"}:
        return f"02-Areas/Family/{person}/Health/Lab/"

    # Imaging
    if dt in {"ultrasound", "imaging", "xray", "mri", "ct", "radiology"}:
        return f"02-Areas/Family/{person}/Health/Diagnostics/"

    # Prescriptions
    if dt in {"prescription", "rx", "medication"}:
        return f"02-Areas/Family/{person}/Health/Prescriptions/"

    # Vision
    if dt in {"eyeexam", "vision", "optometry", "ophthalmology"}:
        return f"02-Areas/Family/{person}/Health/Vision/"

    # Dental
    if dt in {"dental", "dentist", "orthodontics"}:
        return f"02-Areas/Family/{person}/Health/Dental/Records/"

    # Insurance / FMLA / Work
    if dt in {"fmla", "workexcuse", "disability", "insurance", "referral"}:
        return f"02-Areas/Family/{person}/Health/"

    # Default: keep current destination
    return current_dest


def _determine_segments(results: list[dict]) -> list[list[int]]:
    """Group result indices into document segments based on doc_type changes."""
    if not results:
        return []

    # Sort by path (natural order) since we don't have reliable page numbers
    indexed = list(enumerate(results))

    segments = []
    current_seg = [0]

    for i in range(1, len(indexed)):
        prev_dt = results[i - 1].get("docType", "").lower()
        curr_dt = results[i].get("docType", "").lower()

        if prev_dt and curr_dt and not _is_related_type(prev_dt, curr_dt):
            segments.append(current_seg)
            current_seg = [i]
        else:
            current_seg.append(i)

    segments.append(current_seg)
    return segments


def generate_backfill_report(results: list[dict]) -> str:
    """Generate a human-readable report of backfill findings."""
    lines = [f"\n📋 Backfill Report — {len(results)} file(s) analyzed\n"]

    # Group by segment
    segments = _determine_segments(results)

    for seg_idx, seg in enumerate(segments, 1):
        first = results[seg[0]]
        seg_doc_type = first.get("segmentDocType", first.get("docType", "?"))
        seg_provider = first.get("segmentProvider", first.get("provider", "?"))
        seg_date = first.get("scanDate", "?")

        lines.append(f"📄 Document Set #{seg_idx}: {seg_doc_type} ({len(seg)} page{'s' if len(seg) > 1 else ''})")
        lines.append(f"   Provider: {seg_provider} | Date: {seg_date}")

        for idx_in_seg, r_idx in enumerate(seg):
            r = results[r_idx]
            old_name = r["filename"]
            new_name = r.get("proposedName", old_name)
            old_dest = str(Path(r["path"]).parent).replace("/mnt/e/QSync/", "")
            new_dest = r.get("proposedDest", old_dest)
            conf = r.get("confidence", 0)
            cache_tag = "📦" if r.get("ocrFromCache") else "🔍"

            if old_name == new_name and old_dest == new_dest:
                lines.append(f"   {cache_tag} {old_name} → (no change) [{conf*100:.0f}%]")
            else:
                lines.append(f"   {cache_tag} {old_name}")
                lines.append(f"      → {new_name}")
                if old_dest != new_dest:
                    lines.append(f"      → {new_dest}")
                lines.append(f"      [conf: {conf*100:.0f}%]")

        lines.append("")

    # Summary stats
    unchanged = sum(1 for r in results if r["filename"] == r.get("proposedName", r["filename"]))
    changed = len(results) - unchanged
    lines.append(f"📊 Summary: {changed} file(s) need renaming/reorganizing, {unchanged} unchanged")

    return "\n".join(lines)


def execute_moves(
    results: list[dict],
    settings: Settings,
    dry_run: bool = False,
    interactive: bool = False,
) -> dict[str, Any]:
    """Execute the proposed moves."""
    moved = 0
    skipped = 0
    errors = 0

    # Group by segment to move files into subfolders
    segments = _determine_segments(results)

    for seg_idx, seg in enumerate(segments, 1):
        first = results[seg[0]]
        person = first.get("person", "Unknown")
        seg_date = first.get("scanDate", "0000-00-00")
        seg_doc_type = first.get("segmentDocType", first.get("docType", "Document"))

        # Determine target folder
        dest_base = first.get("proposedDest", "")
        if not dest_base:
            dest_base = f"02-Areas/Family/{person}/Health/"

        target_dir = settings.qsync_root / dest_base

        # For multi-document sets, create a subfolder
        if len(seg) > 1 or seg_doc_type in {"DischargeSummary", "HospitalRecord", "FMLA", "SurgicalRecord"}:
            safe_provider = safe_filename_component(first.get("segmentProvider", first.get("provider", "Unknown")))
            safe_type = safe_filename_component(seg_doc_type)
            folder_name = f"{seg_date}_{safe_provider}_{safe_type}".replace("__", "_").strip("_")
            target_dir = target_dir / folder_name

        if not dry_run:
            target_dir.mkdir(parents=True, exist_ok=True)

        for idx_in_seg, r_idx in enumerate(seg):
            r = results[r_idx]
            source = Path(r["path"])
            new_name = r.get("proposedName", source.name)
            target_path = target_dir / new_name

            # Interactive approval
            if interactive:
                print(f"\n  Move: {source.name}")
                print(f"  → {target_dir.name}/{new_name}")
                resp = input("  Approve? [y/n/s] ").strip().lower()
                if resp == "n":
                    skipped += 1
                    continue
                elif resp == "s":
                    skipped += 1
                    break  # Skip entire segment

            if dry_run:
                print(f"  [DRY-RUN] {source.name} → {target_dir.name}/{new_name}")
                continue

            # Handle duplicates in target
            target_path = _unique_path(target_path)

            try:
                # Move the file
                shutil.move(str(source), str(target_path))

                # Move sidecars too
                for suffix in [".meta.json", ".ocr.txt"]:
                    src_sidecar = source.parent / f"{source.stem}{suffix}"
                    if src_sidecar.exists():
                        dst_sidecar = target_path.parent / f"{target_path.stem}{suffix}"
                        shutil.move(str(src_sidecar), str(dst_sidecar))

                # Update DB
                sha = r["sha256"]
                try:
                    from app.state.scan_db import update_lifecycle_approval
                    update_lifecycle_approval(
                        sha256=sha,
                        final_name=new_name,
                        final_dest=str(target_dir.relative_to(settings.qsync_root)),
                        final_doc_type=r.get("docType", ""),
                        final_person=r.get("person", ""),
                        final_provider=r.get("provider", ""),
                        override_type="rename",
                    )
                    log_file_move(str(source), str(target_path), success=True)
                except Exception:
                    pass

                # Update OCR cache path
                try:
                    cached = get_ocr_cache(sha)
                    if cached:
                        save_ocr_cache(sha, str(target_path), new_name, cached["ocr_text"],
                                       cached.get("text_source", "ocr_image"),
                                       cached.get("ocr_duration_ms"),
                                       cached.get("file_size"))
                except Exception:
                    pass

                # Create/update sidecars with corrected metadata
                meta = {
                    "date": r.get("scanDate", ""),
                    "provider": r.get("provider", ""),
                    "description": r.get("docType", ""),
                    "patient": r.get("person", ""),
                    "doc_type": r.get("docType", ""),
                    "ocr_date": datetime.now().strftime("%Y-%m-%d"),
                    "source_file": new_name,
                }
                create_sidecars(target_path, ocr_text=r.get("ocrFullText", ""), meta=meta, overwrite=True)

                # Update health index
                try:
                    update_health_index(
                        settings.qsync_root,
                        target_path,
                        person,
                        r.get("docType", ""),
                        description=r.get("docType", ""),
                        extra_fields={
                            "reason_for_visit": r.get("reason_for_visit", ""),
                            "final_diagnosis": r.get("final_diagnosis", ""),
                            "physician": r.get("physician", ""),
                        },
                    )
                except Exception:
                    pass

                moved += 1
                print(f"  ✅ {source.name} → {target_dir.name}/{target_path.name}")

            except Exception as exc:
                errors += 1
                print(f"  ❌ {source.name}: {exc}")

    # Clean up empty source folders
    if not dry_run and moved > 0:
        _cleanup_empty_source_dirs(results)

    return {"moved": moved, "skipped": skipped, "errors": errors}


def _unique_path(path: Path) -> Path:
    """If path exists, append a counter to make it unique."""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter:03d}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _cleanup_empty_source_dirs(results: list[dict]) -> None:
    """Remove empty directories left behind after moves."""
    seen_parents = set()
    for r in results:
        parent = Path(r["path"]).parent
        seen_parents.add(parent)

    for parent in sorted(seen_parents, key=lambda p: len(p.parts), reverse=True):
        try:
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
                print(f"  🗑️ Removed empty dir: {parent}")
        except Exception:
            pass


def run_backfill(
    target_dir: str,
    settings: Settings,
    fix_sidecars_only: bool = False,
    skip_valid: bool = False,
    dry_run: bool = False,
    interactive: bool = False,
    max_workers: int = 4,
) -> list[dict]:
    """Main backfill entry point."""
    target = Path(target_dir).resolve()
    if not target.exists():
        print(f"❌ Target directory not found: {target}")
        return []

    init_db()
    rules = load_compiled_rules(settings.scan_rules_path)
    file_type_config = load_yaml_config(settings.file_types_path)

    files = collect_target_files(target)
    if not files:
        print(f"📭 No document files found in {target}")
        return []

    print(f"📂 Found {len(files)} document file(s) in {target}")

    # Phase 1: Check sidecars and OCR
    to_process = []
    for f in files:
        has_meta, has_ocr, ocr_text = has_valid_sidecars(f)

        if skip_valid and has_meta and has_ocr:
            continue

        needs_ocr = not has_ocr or not ocr_text
        to_process.append({
            "path": f,
            "has_meta": has_meta,
            "has_ocr": has_ocr,
            "existing_ocr": ocr_text,
            "needs_ocr": needs_ocr,
        })

    if skip_valid:
        skipped = len(files) - len(to_process)
        print(f"   {skipped} file(s) already have valid sidecars, skipping")

    if not to_process:
        print("📭 No files need processing.")
        return []

    print(f"🔍 Processing {len(to_process)} file(s)...")

    # Phase 2: OCR + classify
    results: list[dict] = []
    cache_hits = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for item in to_process:
            f = item["path"]
            future = executor.submit(
                _process_single_backfill,
                f,
                item,
                rules,
                file_type_config,
                settings,
            )
            futures[future] = f

        for future in as_completed(futures):
            f = futures[future]
            try:
                result = future.result(timeout=120)
            except Exception as exc:
                print(f"  ❌ {f.name}: {exc}")
                results.append({
                    "status": "error",
                    "path": str(f),
                    "filename": f.name,
                    "error": str(exc),
                })
                continue

            if result.get("status") == "success":
                if result.get("ocrFromCache"):
                    cache_hits += 1
                    tag = "📦"
                else:
                    tag = "🔍"
                print(f"  {tag} {f.name} → {result.get('docType', '?')} ({result.get('provider', '?')})")
                results.append(result)
            else:
                print(f"  ❌ {f.name}: {result.get('error', '?')}")
                results.append(result)

    if not results:
        print("📭 No files successfully processed.")
        return []

    # Phase 3: Detect multi-document sets and re-propose
    print(f"\n📄 Analyzing {len(results)} result(s) for multi-document sets...")
    results = analyze_multi_document_set(results)

    # Phase 4: Report
    report = generate_backfill_report(results)
    print(report)

    # Phase 5: Fix sidecars only (no moves)
    if fix_sidecars_only:
        fixed = 0
        for r in results:
            if r.get("status") != "success":
                continue
            f = Path(r["path"])
            meta = {
                "date": r.get("scanDate", ""),
                "provider": r.get("provider", ""),
                "description": r.get("docType", ""),
                "patient": r.get("person", ""),
                "doc_type": r.get("docType", ""),
                "ocr_date": datetime.now().strftime("%Y-%m-%d"),
                "source_file": f.name,
            }
            created = create_sidecars(f, ocr_text=r.get("ocrFullText", ""), meta=meta, overwrite=True)
            if created:
                fixed += 1
        print(f"\n✅ Fixed sidecars for {fixed} file(s)")
        return results

    # Phase 6: Execute moves (or dry-run)
    if dry_run:
        print("\n🏃 Dry-run: no files were moved.")
        return results

    print(f"\n🚀 Executing moves...")
    stats = execute_moves(results, settings, dry_run=False, interactive=interactive)
    print(f"\n📊 Moved: {stats['moved']} | Skipped: {stats['skipped']} | Errors: {stats['errors']}")

    # Generate index file if we created subfolders
    for seg in _determine_segments(results):
        first = results[seg[0]]
        if len(seg) > 1:
            # Check if we created a subfolder
            dest_base = first.get("proposedDest", "")
            if dest_base:
                seg_date = first.get("scanDate", "0000-00-00")
                seg_provider = safe_filename_component(first.get("segmentProvider", first.get("provider", "Unknown")))
                seg_type = safe_filename_component(first.get("segmentDocType", first.get("docType", "Document")))
                folder_name = f"{seg_date}_{seg_provider}_{seg_type}".replace("__", "_").strip("_")
                folder_path = settings.qsync_root / dest_base / folder_name
                if folder_path.exists():
                    _write_index_file(folder_path, results, seg)

    return results


def _process_single_backfill(
    file_path: Path,
    item: dict,
    rules,
    file_type_config: dict,
    settings: Settings,
) -> dict:
    """Process a single file for backfill."""
    try:
        # If we have existing OCR text and don't need to re-OCR, use it
        if item["existing_ocr"] and not item["needs_ocr"]:
            extraction = ExtractionResult(
                text=item["existing_ocr"],
                text_source="existing_sidecar",
                pages_inspected=0,
                needs_ocr=False,
                metadata={},
            )
            duration_ms = None
            from_cache = False
        else:
            extraction, duration_ms, from_cache = extract_text(file_path, file_type_config, force_ocr=False)

        scan_date = scan_date_from_mtime(file_path)
        result = classify_and_propose(file_path, extraction, rules, scan_date)
        result["ocrFromCache"] = from_cache
        result["ocrDurationMs"] = duration_ms
        return result

    except Exception as exc:
        return {
            "status": "error",
            "path": str(file_path),
            "filename": file_path.name,
            "error": str(exc),
        }


def _write_index_file(folder_path: Path, all_results: list[dict], segment_indices: list[int]) -> None:
    """Write a 00_INDEX_*.md file in a multi-document folder."""
    segment_results = [all_results[i] for i in segment_indices]
    first = segment_results[0]

    person = first.get("person", "Unknown")
    folder_name = folder_path.name
    index_path = folder_path / f"00_INDEX_{folder_name}.md"

    # Collect diagnostic info from segment
    diagnoses = set()
    providers = set()
    dates = set()
    for r in segment_results:
        if r.get("final_diagnosis"):
            diagnoses.add(r["final_diagnosis"])
        if r.get("provider"):
            providers.add(r.get("provider", ""))
        if r.get("scanDate"):
            dates.add(r["scanDate"])

    lines = [
        f"# 00_INDEX — {folder_name}",
        "",
        f"**Patient:** {person}",
        f"**Folder:** `{folder_path.relative_to(Path('/mnt/e/QSync'))}`",
        "",
        "## Documents",
        "",
    ]

    for r in segment_results:
        fname = Path(r["path"]).name
        dt = r.get("docType", "?")
        prov = r.get("provider", "?")
        lines.append(f"- `{fname}` — {dt} ({prov})")

    if diagnoses:
        lines.extend(["", "## Diagnoses", ""])
        for d in sorted(diagnoses):
            lines.append(f"- {d}")

    if providers:
        lines.extend(["", "## Providers", ""])
        for p in sorted(providers):
            lines.append(f"- {p}")

    if dates:
        lines.extend(["", "## Dates", ""])
        for d in sorted(dates):
            lines.append(f"- {d}")

    lines.append("")
    lines.append("---")
    lines.append(f"*Index auto-generated on {datetime.now().strftime('%Y-%m-%d')}*")
    lines.append("")

    index_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  📝 Created index: {index_path.name}")
