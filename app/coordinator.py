from __future__ import annotations

from datetime import datetime

from app.safe_move import safe_move_file, list_failed_moves, recover_failed_move
from pathlib import Path
from typing import Any

import json

from app.index_manager import update_health_index
from app.pipeline import process_batch
from app.settings import Settings
from app.sidecar import build_meta_from_scan_result, create_sidecars
from app.state.store import StateStore


def run_scan_batch(settings: Settings, explicit_files: list[str] | None = None, batch_id: str | None = None) -> dict[str, Any]:
    files = [Path(file_path) for file_path in explicit_files] if explicit_files else list_inbox_files(settings)
    return process_batch(settings, files, batch_id=batch_id)


def list_inbox_files(settings: Settings) -> list[Path]:
    if not settings.inbox_root.exists():
        return []
    return sorted(path for path in settings.inbox_root.rglob("*") if path.is_file())


def approve_proposal(
    settings: Settings,
    sha256: str,
    override_dest: str | None = None,
    override_name: str | None = None,
) -> dict[str, Any]:
    store = StateStore(settings.state_dir, settings.rule_suggestions_path)
    proposal = store.get_proposal(sha256)
    if not proposal:
        return {"ok": False, "error": "unknown sha256", "sha256": sha256}

    source = Path(proposal["path"])
    if not source.exists():
        return {"ok": False, "error": "source missing", "sha256": sha256}

    proposal_data = proposal["proposal"]
    final_dest = override_dest or proposal_data["proposedDest"]
    final_name = override_name or proposal_data["proposedName"]
    target_dir = settings.qsync_root / final_dest
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = _unique_path(target_dir / final_name)
    move_result = safe_move_file(source, target_path)
    if not move_result["ok"]:
        # Move failed — update lifecycle and return error
        try:
            from app.state.scan_db import update_lifecycle_approval
            update_lifecycle_approval(
                sha256=sha256,
                final_name=final_name,
                final_dest=final_dest,
                final_doc_type=proposal_data.get("docType", ""),
                final_person=proposal_data.get("person", ""),
                final_provider=proposal_data.get("provider", ""),
                override_type="move_failed",
                rejection_reason=move_result["error"],
            )
        except Exception:
            pass
        return {"ok": False, "error": f"Move failed: {move_result['error']}", "sha256": sha256, "move_error": move_result}
    target_path = Path(move_result["moved_to"])

    # Generate sidecar files (.ocr.txt + .meta.json) next to the moved file
    _create_sidecars_for_approved(settings, sha256, target_path)

    # Update person's health index file
    _update_index_for_approved(settings, sha256, target_path, final_dest, proposal_data)

    # Clean up empty parent folders left behind in the inbox
    _cleanup_empty_parents(source.parent, settings.inbox_root)

    feedback_type = "approve"
    if override_dest and override_dest != proposal_data["proposedDest"]:
        feedback_type = "override_destination"
    elif override_name and override_name != proposal_data["proposedName"]:
        feedback_type = "rename_correction"

    store.record_feedback(
        {
            "type": feedback_type,
            "sha256": sha256,
            "ruleMatchId": proposal_data.get("ruleMatchId"),
            "fromDest": proposal_data["proposedDest"],
            "toDest": final_dest,
            "fromName": proposal_data["proposedName"],
            "toName": final_name,
            "approvedAt": datetime.now().isoformat(timespec="seconds"),
        }
    )
    store.save_proposal(
        sha256,
        {
            **proposal,
            "status": "approved",
            "approvedAt": datetime.now().isoformat(timespec="seconds"),
            "movedTo": str(target_path),
            "proposal": {**proposal_data, "proposedDest": final_dest, "proposedName": final_name},
        },
    )
    return {"ok": True, "sha256": sha256, "movedTo": str(target_path)}


def deny_proposal(settings: Settings, sha256: str, reason: str) -> dict[str, Any]:
    store = StateStore(settings.state_dir, settings.rule_suggestions_path)
    proposal = store.get_proposal(sha256)
    if not proposal:
        return {"ok": False, "error": "unknown sha256", "sha256": sha256}

    store.record_feedback(
        {
            "type": "deny",
            "sha256": sha256,
            "ruleMatchId": proposal["proposal"].get("ruleMatchId"),
            "reason": reason,
            "deniedAt": datetime.now().isoformat(timespec="seconds"),
        }
    )
    store.save_proposal(
        sha256,
        {
            **proposal,
            "status": "denied",
            "denyReason": reason,
            "deniedAt": datetime.now().isoformat(timespec="seconds"),
        },
    )
    return {"ok": True, "sha256": sha256, "reason": reason}


def _unique_path(path: Path) -> Path:
    """Find a unique path, truncating filename if full path exceeds 240 chars."""
    path = _truncate_long_path(path)
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    index = 2
    while True:
        candidate = path.with_name(f"{stem}_v{index}{suffix}")
        candidate = _truncate_long_path(candidate)
        if not candidate.exists():
            return candidate
        index += 1


def _truncate_long_path(path: Path) -> Path:
    """Truncate filename if full path exceeds 240 chars (Windows MAX_PATH 260 - 20 buffer).

    If the path is too long, truncates the description segment in the filename
    and adds a '…' marker. Logs the truncation.
    Returns the (possibly truncated) path.
    """
    import logging
    path_str = str(path)
    if len(path_str) <= 240:
        return path

    logger = logging.getLogger(__name__)
    parent_str = str(path.parent)
    max_filename_len = 240 - len(parent_str) - 1  # -1 for the separator
    if max_filename_len < 10:
        logger.warning(f"Path too long ({len(path_str)} chars), parent dir alone is {len(parent_str)} chars: {path_str}")
        return path

    original_name = path.name
    stem = path.stem
    suffix = path.suffix
    available = max_filename_len - len(suffix) - 1  # -1 for … marker
    if available < 5:
        truncated_stem = stem[:max_filename_len - len(suffix)]
    else:
        truncated_stem = stem[:available] + "…"
    new_path = path.parent / f"{truncated_stem}{suffix}"
    logger.warning(f"Path truncated from {len(path_str)} to {len(str(new_path))} chars: {original_name} → {new_path.name}")
    return new_path


def _create_sidecars_for_approved(settings: Settings, sha256: str, target_path: Path) -> list[Path]:
    """Generate .ocr.txt and .meta.json sidecars for an approved+moved file."""
    store = StateStore(settings.state_dir, settings.rule_suggestions_path)
    proposal = store.get_proposal(sha256)
    if not proposal:
        return []

    # Get OCR text from the batch results — prefer full text over sample
    ocr_text = proposal.get("ocrFullText", "") or proposal.get("ocrSample", "")

    # Try to find full OCR text from last_batch.json or last_scan_results.json
    if not ocr_text or len(ocr_text) < 50:
        for json_name in ("last_batch.json", "last_scan_results.json"):
            json_path = settings.state_dir / json_name
            if json_path.exists():
                try:
                    batch_data = json.loads(json_path.read_text(encoding="utf-8"))
                    results = batch_data.get("results", batch_data if isinstance(batch_data, list) else [])
                    for r in results:
                        if r.get("sha256") == sha256:
                            ocr_text = r.get("ocrFullText", r.get("ocrSample", ocr_text))
                            break
                except Exception:
                    pass

    # Build structured metadata from the scan result
    meta = build_meta_from_scan_result(proposal.get("proposal", {}))
    # Enrich with proposal-level fields
    meta["proposed_name"] = proposal["proposal"].get("proposedName", "")
    meta["proposed_dest"] = proposal["proposal"].get("proposedDest", "")

    return create_sidecars(target_path, ocr_text=ocr_text, meta=meta)


def _update_index_for_approved(settings: Settings, sha256: str, target_path: Path, dest: str, proposal_data: dict) -> Path | None:
    """Update the person's health index file after approval."""
    person = proposal_data.get("person", "")
    doc_type = proposal_data.get("docType", "")

    # Only update index for health-related destinations
    if "Family" not in dest and "Health" not in dest:
        return None

    if not person:
        return None

    # Try to get extra fields from the full scan result
    extra_fields = {}
    store = StateStore(settings.state_dir, settings.rule_suggestions_path)
    proposal = store.get_proposal(sha256)
    if proposal:
        # Look for enriched fields in batch results
        for json_name in ("last_batch.json", "last_scan_results.json"):
            json_path = settings.state_dir / json_name
            if json_path.exists():
                try:
                    batch_data = json.loads(json_path.read_text(encoding="utf-8"))
                    results = batch_data.get("results", batch_data if isinstance(batch_data, list) else [])
                    for r in results:
                        if r.get("sha256") == sha256:
                            for field in ("reason_for_visit", "final_diagnosis", "physician", "facility", "medication", "brandName"):
                                if r.get(field):
                                    extra_fields[field] = r[field]
                            break
                except Exception:
                    pass

    try:
        return update_health_index(
            settings.qsync_root,
            target_path,
            person,
            doc_type,
            description=proposal_data.get("proposedName", ""),
            extra_fields=extra_fields if extra_fields else None,
        )
    except Exception as e:
        print(f"  ⚠️ Index update failed: {e}")
        return None


def _cleanup_empty_parents(start_dir: Path, stop_dir: Path) -> list[str]:
    """Remove empty directories from start_dir up to (but not including) stop_dir.

    Only deletes directories that are empty after file removal.
    Returns list of removed directory paths.
    """
    removed = []
    current = start_dir.resolve()
    stop = stop_dir.resolve()
    while current != stop and current.is_dir():
        try:
            if not any(current.iterdir()):
                current.rmdir()
                removed.append(str(current))
                current = current.parent
            else:
                break  # Directory not empty, stop here
        except OSError:
            break  # Permission error or other issue, stop
    return removed
