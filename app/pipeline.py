from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.classify.config import load_compiled_rules, load_yaml_config
from app.classify.engine import classify_document
from app.duplicates.index import DuplicateIndex
from app.extractors.docx import extract_docx
from app.extractors.images import extract_image
from app.extractors.pdf import extract_pdf
from app.extractors.xlsx import extract_xlsx
from app.notifications.render import render_notification
from app.settings import Settings
from app.state.store import StateStore
from app.utils import SUPPORTED_EXTENSIONS, normalize_spaces, scan_date_from_mtime, sha256_file


def process_batch(settings: Settings, files: list[Path], batch_id: str | None = None) -> dict[str, Any]:
    file_type_config = load_yaml_config(settings.file_types_path)
    notification_config = load_yaml_config(settings.notifications_path)
    allowed_duplicate_exts = {ext.lower() for ext in file_type_config.get("duplicates", {}).get("indexed_extensions", [])}
    supported = {ext.lower() for ext in file_type_config.get("supported_extensions", [])}
    filtered_files = _normalize_files(files, supported)
    actual_batch_id = batch_id or f"batch-{uuid4().hex[:12]}"
    store = StateStore(settings.state_dir, settings.rule_suggestions_path)

    duplicate_index = DuplicateIndex(settings.qsync_root, allowed_duplicate_exts)
    # Use SQLite-based file index for duplicate checking instead of walking QSync.
    # The SQLite index is much faster than walking the 9P filesystem.
    # Build/update the index if SCAN_BUILD_INDEX=1 or if index is empty.
    from app.state.scan_db import check_duplicate_index, init_db
    init_db()  # Ensure file_index table exists
    use_sql_index = True
    import os
    if os.environ.get("SCAN_BUILD_INDEX", "").strip() == "1":
        from app.state.scan_db import build_file_index
        idx_stats = build_file_index(str(settings.qsync_root))
        print(f"📊 File index built: {idx_stats}")
    elif len(filtered_files) > 1:
        # Only build index for batch scans if not already built
        pass

    if os.environ.get("SCAN_BUILD_INDEX", "").strip() == "1":
        duplicate_index.build()
    else:
        pass  # Empty index — duplicate checks use SQLite instead

    max_workers = min(
        settings.max_workers,
        int(file_type_config.get("worker_pool", {}).get("default_max_workers", settings.max_workers)),
    )
    results: list[dict[str, Any]] = []
    
    # For single files, skip ProcessPool overhead and run directly
    if len(filtered_files) == 1:
        results.append(_process_single_file(
            str(filtered_files[0]), 
            str(settings.scan_rules_path), 
            json.dumps(file_type_config)
        ))
    else:
        # Process files sequentially by default (safer in WSL/PipelinePoolExecutor hangs)
        # Set SCAN_PARALLEL=1 to enable ProcessPoolExecutor
        import os
        use_parallel = os.environ.get("SCAN_PARALLEL", "").strip() == "1"
        if use_parallel and max_workers > 1:
            try:
                with ProcessPoolExecutor(max_workers=max_workers) as executor:
                    future_map = {
                        executor.submit(_process_single_file, str(file_path), str(settings.scan_rules_path), json.dumps(file_type_config)): file_path
                        for file_path in filtered_files
                    }
                    for future in as_completed(future_map, timeout=300):
                        results.append(future.result())
            except Exception as exc:
                # ProcessPoolExecutor can hang/deadlock in WSL — fall back to sequential
                import logging
                logging.warning(f"ProcessPoolExecutor failed ({exc}), falling back to sequential processing")
                for file_path in filtered_files:
                    results.append(_process_single_file(
                        str(file_path),
                        str(settings.scan_rules_path),
                        json.dumps(file_type_config),
                    ))
        else:
            for file_path in filtered_files:
                results.append(_process_single_file(
                    str(file_path),
                    str(settings.scan_rules_path),
                    json.dumps(file_type_config),
                ))

    for i, result in enumerate(results, start=1):
        result["id"] = i
        if result.get("status") != "success":
            continue
        source = Path(result["path"])
        proposed_dest = result.get("proposedDest", "")
        restrict_root = (settings.qsync_root / proposed_dest).resolve() if proposed_dest else None
        # Check duplicates using SQLite index (fast) or DuplicateIndex (slow)
        if use_sql_index and result.get("sha256"):
            # Build sidecar data for fuzzy matching
            _ocr_hash = None
            _meta_fields = None
            try:
                from app.state.scan_db import get_ocr_cache
                import hashlib
                cache = get_ocr_cache(result.get("sha256", ""))
                if cache and cache.get("ocr_text"):
                    _ocr_hash = hashlib.sha256(cache["ocr_text"].encode()).hexdigest()[:16]
            except Exception:
                pass
            # Build meta fields from classification result for fuzzy matching
            if result.get("provider") or result.get("person") or result.get("docType"):
                _meta_fields = {
                    "provider": result.get("provider", ""),
                    "date": result.get("docDate", ""),
                    "patient": result.get("person", ""),
                    "doc_type": result.get("docType", ""),
                    "description": result.get("proposedName", ""),
                }
            sql_dups = check_duplicate_index(
                result["sha256"],
                str(settings.qsync_root / proposed_dest) if proposed_dest else None,
                ocr_hash=_ocr_hash,
                meta_fields=_meta_fields,
            )
            result["contentDuplicates"] = [d["path"] for d in sql_dups if d.get("path")]
            result["duplicateMatchTypes"] = [d.get("match_type", "exact") for d in sql_dups]
            result["duplicateMatchScores"] = {d["path"]: d.get("match_score") for d in sql_dups if d.get("match_score") is not None}
            result["duplicatesAnywhere"] = result["contentDuplicates"]
        elif len(filtered_files) > 1:
            result["contentDuplicates"] = duplicate_index.find_exact_duplicates(source, restrict_to=restrict_root)
            result["duplicatesAnywhere"] = duplicate_index.find_exact_duplicates(source)
        else:
            result["contentDuplicates"] = []
            result["duplicatesAnywhere"] = []
        store.save_proposal(
            result["sha256"],
            {
                "batchId": actual_batch_id,
                "path": result["path"],
                "filename": result["filename"],
                "timestamp": datetime.now().isoformat(),
                "row_number": result.get("id"),
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

    notification = render_notification(actual_batch_id, results, notification_config)

    # ── Auto-approve logic ──
    auto_approve_config = file_type_config.get("auto_approve", {})
    if auto_approve_config.get("enabled", False):
        from app.classify.config import load_yaml_config
        rules_config = load_yaml_config(settings.scan_rules_path)
        aa_config = rules_config.get("auto_approve", {})
        threshold = aa_config.get("threshold", 0.90)
        never_types = set(aa_config.get("never_auto_approve", []))
        safe_types = set(aa_config.get("safe_types", []))
        for result in results:
            if result.get("status") != "success":
                result["autoApproved"] = False
                continue
            doc_type = result.get("docType", "")
            cls_conf = result.get("classificationConfidence", result.get("confidence", 0))
            person = result.get("person", "")
            has_dup = bool(result.get("contentDuplicates") or result.get("duplicatesAnywhere"))
            ambiguous = result.get("ambiguous")
            needs_side = result.get("needsSideConfirmation", False)
            # Conditions for auto-approve:
            # 1. Classification confidence >= threshold
            # 2. Doc type is in safe list (or safe_types is empty = all safe)
            # 3. Doc type is NOT in never list
            # 4. No duplicates found
            # 5. No ambiguous classification
            # 6. No side confirmation needed
            # 7. Person detected (not "Unknown")
            can_auto = (
                cls_conf >= threshold
                and doc_type not in never_types
                and (not safe_types or doc_type in safe_types)
                and not has_dup
                and not ambiguous
                and not needs_side
                and person and person != "Unknown"
            )
            result["autoApproved"] = can_auto
            if can_auto:
                result["_auto_approve_note"] = f"Auto-eligible: cls_conf={cls_conf:.2f} >= {threshold}"
    else:
        for result in results:
            result["autoApproved"] = False

    # ── Lifecycle tracking: record classification proposal for each file ──
    try:
        from app.state.scan_db import save_lifecycle, get_ocr_cache
        for result in results:
            if result.get("status") != "success":
                continue
            ocr_text_hash = ""
            try:
                cache = get_ocr_cache(result.get("sha256", ""))
                if cache and cache.get("ocr_text"):
                    import hashlib
                    ocr_text_hash = hashlib.sha256(cache["ocr_text"].encode()).hexdigest()[:16]
            except Exception:
                pass
            save_lifecycle(
                sha256=result.get("sha256", ""),
                original_filename=result.get("filename", ""),
                original_path=result.get("path", ""),
                file_size=0,
                ocr_text_hash=ocr_text_hash,
                text_source=result.get("textSource", ""),
                text_quality=result.get("textQuality", 0),
                proposed_name=result.get("proposedName", ""),
                proposed_dest=result.get("proposedDest", ""),
                proposed_doc_type=result.get("docType", ""),
                proposed_person=result.get("person", ""),
                proposed_provider=result.get("provider", ""),
                classification_confidence=result.get("classificationConfidence", result.get("confidence", 0)),
                rule_match_id=result.get("ruleMatchId", ""),
            )
    except Exception:
        pass  # Don't fail the pipeline if lifecycle tracking fails

    payload = {
        "status": "complete",
        "batchId": actual_batch_id,
        "processed": len(results),
        "successes": sum(1 for item in results if item.get("status") == "success"),
        "failures": sum(1 for item in results if item.get("status") != "success"),
        "results": results,
        "notification": notification,
    }
    store.save_batch(actual_batch_id, payload)

    # Save to scan_history.db
    try:
        from app.state.scan_db import save_scan_session
        save_scan_session(results, f"Scan batch {actual_batch_id}")
    except Exception:
        pass  # Don't fail if DB is unavailable

    return payload


def _normalize_files(files: list[Path], supported: set[str]) -> list[Path]:
    seen: set[Path] = set()
    normalized: list[Path] = []
    for file_path in files:
        resolved = file_path.resolve()
        if resolved in seen or not resolved.exists() or not resolved.is_file():
            continue
        if supported and resolved.suffix.lower() not in supported:
            continue
        seen.add(resolved)
        normalized.append(resolved)
    return normalized


def _process_single_file(file_path: str, scan_rules_path: str, file_type_config_json: str) -> dict[str, Any]:
    file_types = json.loads(file_type_config_json)
    rules = load_compiled_rules(Path(scan_rules_path))
    path = Path(file_path)
    result: dict[str, Any] = {
        "status": "processing",
        "path": str(path),
        "filename": path.name,
    }
    try:
        if path.suffix.lower() == ".pdf":
            extraction = extract_pdf(
                path,
                inspect_pages=int(file_types["pdf"]["inspect_pages"]),
                min_text_chars_before_skip_ocr=int(file_types["pdf"]["min_text_chars_before_skip_ocr"]),
                render_dpi=int(file_types["pdf"]["render_dpi"]),
            )
        elif path.suffix.lower() in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}:
            extraction = extract_image(path, int(file_types["images"]["max_ocr_chars"]))
        elif path.suffix.lower() == ".docx":
            extraction = extract_docx(
                path,
                max_paragraphs=int(file_types["docx"]["max_paragraphs"]),
                max_table_rows=int(file_types["docx"]["max_table_rows"]),
            )
        elif path.suffix.lower() == ".xlsx":
            extraction = extract_xlsx(
                path,
                max_sheets=int(file_types["xlsx"]["max_sheets"]),
                max_rows_per_sheet=int(file_types["xlsx"]["max_rows_per_sheet"]),
            )
        else:
            raise RuntimeError(f"Unsupported file type: {path.suffix}")

        sha = sha256_file(path)
        scan_date = scan_date_from_mtime(path)

        # ── OCR quality assessment ──
        from app.extractors.quality import assess_text_quality
        text_quality = assess_text_quality(extraction.text)

        # If PDF text quality is poor, try re-extracting with Tesseract
        re_extracted = False
        if path.suffix.lower() == ".pdf" and text_quality < 0.3 and extraction.text_source != "ocr_image":
            try:
                from app.extractors.images import extract_image
                # Re-render pages as images and OCR with Tesseract
                render_dpi = int(file_types.get("pdf", {}).get("render_dpi", 200))
                re_extraction = extract_pdf(
                    path,
                    inspect_pages=int(file_types["pdf"]["inspect_pages"]),
                    min_text_chars_before_skip_ocr=0,  # Force OCR
                    render_dpi=render_dpi,
                )
                re_quality = assess_text_quality(re_extraction.text)
                if re_quality > text_quality:
                    extraction = re_extraction
                    text_quality = re_quality
                    re_extracted = True
            except Exception:
                pass  # Keep original extraction if re-extraction fails

        classification = classify_document(extraction.text, path, scan_date, rules)
        result.update(
            {
                "status": "success",
                "sha256": sha,
                "scanDate": scan_date,
                "fileType": path.suffix.lower().lstrip("."),
                "textSource": extraction.text_source,
                "pagesInspected": extraction.pages_inspected,
                "needsOcr": extraction.needs_ocr,
                "ocrReExtracted": re_extracted,
                "textQuality": text_quality,
                "ocrSample": normalize_spaces(extraction.text)[:240],
                "highlights": classification.highlights,
                "docType": classification.doc_type,
                "person": classification.person,
                "provider": classification.provider,
                "proposedName": classification.proposed_name,
                "proposedDest": classification.proposed_dest,
                "confidence": classification.confidence,
                "classificationConfidence": classification.classification_confidence,
                "ruleMatchId": classification.rule_match_id,
                "metadata": extraction.metadata,
                "expDate": classification.exp_date,
                "side": classification.side,
                "sideConfidence": classification.side_confidence,
                "needsSideConfirmation": classification.needs_side_confirmation,
                "ambiguous": classification.ambiguous,
                "question": classification.question,
                "medication": classification.medication,
                "brandName": classification.brand_name,
            }
        )
    except Exception as exc:
        result.update(
            {
                "status": "error",
                "error": str(exc),
                "errorType": type(exc).__name__,
            }
        )
    return result
