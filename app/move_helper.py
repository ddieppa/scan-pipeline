#!/usr/bin/env python3
"""Move helper — move a single scan file to QSync with sidecars and index update.

Usage:
    # Move a file with manual classification
    python3 -m app.move_helper move /src/file.jpg --dest "02-Areas/Family/Daniel/Health/Prescriptions/" --name "2020-11-12_Walgreens_Rx_Daniel.jpg" --person Daniel --provider Walgreens --doc-type Rx --date 2020-11-12

    # Move with OCR text (will create .ocr.txt sidecar)
    python3 -m app.move_helper move /src/file.jpg --dest "02-Areas/Family/Daniel/Health/Prescriptions/" --name "2020-11-12_Walgreens_Rx_Daniel.jpg" --person Daniel --ocr-text "Full OCR text here..."

    # Auto-OCR the source file before moving (extracts text via pipeline extractors)
    python3 -m app.move_helper move /src/file.jpg --dest "..." --name "..." --person Daniel --auto-ocr

    # Move and also create sidecars for already-moved files (backfill)
    python3 -m app.move_helper backfill /path/to/QSync/folder/
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Ensure pipeline root is importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.sidecar import build_meta_from_scan_result, create_sidecars
from app.index_manager import update_health_index
from app.utils import sha256_file, normalize_spaces


def move_file(
    source: Path,
    dest_rel: str,
    name: str,
    person: str,
    provider: str = "Unknown",
    doc_type: str = "Unknown",
    doc_date: str = "",
    ocr_text: str | None = None,
    auto_ocr: bool = False,
    qsync_root: Path | None = None,
    description: str = "",
    extra_fields: dict | None = None,
    copy_mode: bool = False,
    source_folder: str = "",
) -> dict[str, Any]:
    """Move a source file to QSync with sidecars and index update.

    Args:
        source: Path to the source file.
        dest_rel: Relative destination under QSync root (e.g., "02-Areas/Family/Daniel/Health/Prescriptions/").
        name: Final filename for the moved file.
        person: Person name (Daniel, Natalie, Isabella, etc.).
        provider: Provider name.
        doc_type: Document type.
        doc_date: Document date (YYYY-MM-DD).
        ocr_text: Pre-extracted OCR text.
        auto_ocr: If True, extract OCR from the source file before moving.
        qsync_root: Path to QSync root. Defaults to /mnt/e/QSync.
        description: Optional description for index entry.
        extra_fields: Optional extra metadata fields.
        copy_mode: If True, copy instead of move.
        source_folder: Original source folder path (for provenance).

    Returns:
        Dict with result details.
    """
    if not source.exists():
        return {"ok": False, "error": f"source not found: {source}"}

    if qsync_root is None:
        qsync_root = Path("/mnt/e/QSync")

    # Auto-OCR if requested
    if auto_ocr and not ocr_text:
        ocr_text = _extract_ocr(source)

    # Build target path
    target_dir = qsync_root / dest_rel.rstrip("/")
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / name

    # Handle existing target
    if target_path.exists():
        # Don't overwrite — add version suffix
        stem = target_path.stem
        suffix = target_path.suffix
        idx = 2
        while target_path.exists():
            target_path = target_dir / f"{stem}_v{idx}{suffix}"
            idx += 1

    # Move or copy
    if copy_mode:
        shutil.copy2(str(source), str(target_path))
    else:
        shutil.move(str(source), str(target_path))

    # Build metadata
    meta = {
        "date": doc_date,
        "provider": provider,
        "description": description or f"{doc_type} for {person}",
        "patient": person,
        "doc_type": doc_type,
        "ocr_date": datetime.now().strftime("%Y-%m-%d"),
        "source_file": source.name,
    }
    if source_folder:
        meta["source_folder"] = source_folder
    if extra_fields:
        meta.update(extra_fields)

    # Create sidecars
    sidecars = create_sidecars(target_path, ocr_text=ocr_text, meta=meta)

    # Update index (only for health-related destinations)
    index_path = None
    if "Family" in dest_rel and "Health" in dest_rel:
        try:
            index_path = update_health_index(
                qsync_root, target_path, person, doc_type,
                description=name, extra_fields=extra_fields,
            )
        except Exception as e:
            print(f"  ⚠️ Index update failed: {e}")

    # Cleanup empty parent folders if we moved (not copied)
    if not copy_mode:
        _cleanup_empty_parents(source.parent, source.parent.parents)

    return {
        "ok": True,
        "moved_to": str(target_path),
        "sidecars": [str(p) for p in sidecars],
        "index": str(index_path) if index_path else None,
    }


def _extract_ocr(path: Path) -> str | None:
    """Extract OCR text from a file using pipeline extractors."""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            from app.extractors.pdf import extract_pdf
            result = extract_pdf(path, inspect_pages=10, min_text_chars_before_skip_ocr=20, render_dpi=200)
            return result.text if hasattr(result, 'text') else str(result)
        elif ext in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}:
            from app.extractors.images import extract_image
            result = extract_image(path, 20000)
            return result.text if hasattr(result, 'text') else str(result)
        elif ext == ".docx":
            from app.extractors.docx import extract_docx
            result = extract_docx(path, 400, 200)
            return result.text if hasattr(result, 'text') else str(result)
    except Exception as e:
        print(f"  ⚠️ OCR extraction failed: {e}")
    return None


def _cleanup_empty_parents(start: Path, stop_at: list[Path]) -> None:
    """Remove empty parent directories up to inbox root."""
    # Don't go above a reasonable stopping point
    for parent in [start] + list(start.parents):
        if parent in stop_at or not parent.exists():
            break
        try:
            if not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            break


def backfill_sidecars(root: Path, qsync_root: Path | None = None) -> list[dict]:
    """Generate sidecars for all document files in a directory that are missing them.

    Also updates the index for any health-related folders.

    Returns list of results.
    """
    from app.sidecar import find_documents_without_sidecars
    if qsync_root is None:
        qsync_root = Path("/mnt/e/QSync")

    missing = find_documents_without_sidecars(root)
    results = []

    for f in missing:
        print(f"  📄 {f.name} — missing sidecars")
        # Extract OCR
        ocr_text = _extract_ocr(f)
        if not ocr_text:
            ocr_text = f"[OCR extraction not available for {f.suffix}]"

        # Parse metadata from filename convention: YYYY-MM-DD_Provider_Type_Person.ext
        meta = _parse_meta_from_filename(f)

        # Create sidecars
        sidecars = create_sidecars(f, ocr_text=ocr_text, meta=meta)
        results.append({
            "file": str(f),
            "sidecars": [str(p) for p in sidecars],
        })

    # Update indexes for health folders
    for person in ["Daniel", "Natalie", "Isabella", "Grisell"]:
        health_dir = qsync_root / "02-Areas" / "Family" / person / "Health"
        if health_dir.exists():
            try:
                update_health_index(qsync_root, health_dir, person, "backfill", description="sidecar backfill")
            except Exception:
                pass

    return results


def _parse_meta_from_filename(f: Path) -> dict[str, Any]:
    """Parse metadata from a filename following the YYYY-MM-DD_Provider_Type_Person convention."""
    stem = f.stem
    meta = {
        "source_file": f.name,
        "ocr_date": datetime.now().strftime("%Y-%m-%d"),
    }

    # Try to extract date
    import re
    date_match = re.match(r"(\d{4}-\d{2}-\d{2})", stem)
    if date_match:
        meta["date"] = date_match.group(1)

    # Try to extract person (last segment before extension)
    parts = stem.split("_")
    if len(parts) >= 2:
        meta["patient"] = parts[-1]
    if len(parts) >= 3:
        meta["provider"] = parts[1]

    return meta


def main():
    parser = argparse.ArgumentParser(description="Move helper — move files with sidecars and index updates")
    subparsers = parser.add_subparsers(dest="command")

    # move command
    move_parser = subparsers.add_parser("move", help="Move a file to QSync with sidecars")
    move_parser.add_argument("source", type=Path, help="Source file path")
    move_parser.add_argument("--dest", required=True, help="Relative destination under QSync")
    move_parser.add_argument("--name", required=True, help="Final filename")
    move_parser.add_argument("--person", required=True, help="Person name")
    move_parser.add_argument("--provider", default="Unknown")
    move_parser.add_argument("--doc-type", default="Unknown")
    move_parser.add_argument("--date", default="")
    move_parser.add_argument("--ocr-text", default=None, help="OCR text (or - for stdin)")
    move_parser.add_argument("--auto-ocr", action="store_true", help="Extract OCR before moving")
    move_parser.add_argument("--copy", action="store_true", help="Copy instead of move")
    move_parser.add_argument("--source-folder", default="")
    move_parser.add_argument("--description", default="")
    move_parser.add_argument("--extra", default=None, help="Extra fields as JSON")

    # backfill command
    backfill_parser = subparsers.add_parser("backfill", help="Generate sidecars for files missing them")
    backfill_parser.add_argument("directory", type=Path, help="Directory to scan")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "move":
        extra = json.loads(args.extra) if args.extra else None
        ocr_text = args.ocr_text
        if ocr_text == "-":
            ocr_text = sys.stdin.read()

        result = move_file(
            source=args.source,
            dest_rel=args.dest,
            name=args.name,
            person=args.person,
            provider=args.provider,
            doc_type=args.doc_type,
            doc_date=args.date,
            ocr_text=ocr_text,
            auto_ocr=args.auto_ocr,
            copy_mode=args.copy,
            source_folder=args.source_folder,
            description=args.description,
            extra_fields=extra,
        )
        if result["ok"]:
            print(f"✅ Moved to: {result['moved_to']}")
            for s in result.get("sidecars", []):
                print(f"   Sidecar: {s}")
            if result.get("index"):
                print(f"   Index: {result['index']}")
        else:
            print(f"❌ Error: {result['error']}")

    elif args.command == "backfill":
        results = backfill_sidecars(args.directory)
        print(f"\n📊 Backfill complete: {len(results)} files processed")


if __name__ == "__main__":
    main()