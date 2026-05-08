#!/usr/bin/env python3
"""Sidecar generator — creates .ocr.txt and .meta.json files next to moved documents.

Called after a file is approved and moved to its final destination.
Generates sidecar metadata files that travel with the document for future
search, reuse, and AI processing.

Sidecar files:
  - .ocr.txt  — Full OCR text extracted from the source file
  - .meta.json — Structured metadata (provider, date, person, amount, doc_type, etc.)

Usage:
    # Generate sidecars for a single moved file
    python3 sidecar.py create /path/to/moved/file.jpg --ocr-text "..." --meta '{"provider": "...", ...}'

    # Generate sidecars for all files in a directory that lack them
    python3 sidecar.py backfill /path/to/directory

    # Check sidecar coverage for a directory tree
    python3 sidecar.py status /path/to/directory
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


# ── Sidecar creation ──────────────────────────────────────────────

def create_sidecars(
    file_path: Path,
    ocr_text: str | None = None,
    meta: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> list[Path]:
    """Create .ocr.txt and .meta.json sidecars next to a file.

    Args:
        file_path: Path to the document file.
        ocr_text: Raw OCR text to save. If None, .ocr.txt is skipped.
        meta: Structured metadata dict. If None, .meta.json is skipped.
        overwrite: If True, overwrite existing sidecars.

    Returns:
        List of sidecar paths that were created.
    """
    if not file_path.exists():
        print(f"  ⚠️  Source file not found: {file_path}")
        return []

    created = []
    stem = file_path.stem
    parent = file_path.parent

    # .ocr.txt
    if ocr_text is not None:
        ocr_path = parent / f"{stem}.ocr.txt"
        if overwrite or not ocr_path.exists():
            ocr_path.write_text(ocr_text.strip() + "\n", encoding="utf-8")
            created.append(ocr_path)

    # .meta.json
    if meta is not None:
        meta_path = parent / f"{stem}.meta.json"
        if overwrite or not meta_path.exists():
            # Ensure ocr_date is set
            if "ocr_date" not in meta:
                meta["ocr_date"] = datetime.now().strftime("%Y-%m-%d")
            # Ensure source_file references the original
            if "source_file" not in meta:
                meta["source_file"] = file_path.name
            meta_path.write_text(
                json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            created.append(meta_path)

    return created


def build_meta_from_scan_result(result: dict) -> dict[str, Any]:
    """Build a .meta.json dict from a scan_workflow scan result.

    Maps the scan pipeline output fields to structured metadata.
    """
    meta: dict[str, Any] = {
        "provider": result.get("provider", ""),
        "date": result.get("scanDate", ""),
        "patient": result.get("person", ""),
        "doc_type": result.get("docType", ""),
        "description": "",
        "confidence": result.get("confidence", 0),
        "rule_match": result.get("ruleMatchId", ""),
        "source_file": result.get("filename", ""),
        "ocr_date": datetime.now().strftime("%Y-%m-%d"),
    }

    # Add medication info if present
    if result.get("medication"):
        meta["medication"] = result["medication"]
    if result.get("brandName"):
        meta["brand_name"] = result["brandName"]

    # Build a human-readable description
    parts = []
    if meta["provider"]:
        parts.append(meta["provider"])
    if meta["doc_type"]:
        parts.append(meta["doc_type"])
    if meta["patient"]:
        parts.append(f"for {meta['patient']}")
    if parts:
        meta["description"] = " ".join(parts)

    # Add clinical enrichment fields
    for field in ("reason_for_visit", "final_diagnosis", "physician", "facility", "fin", "mrn"):
        if result.get(field):
            meta[field] = result[field]

    return meta


# ── Backfill ───────────────────────────────────────────────────────

def find_documents_without_sidecars(root: Path, extensions: set[str] | None = None) -> list[Path]:
    """Find document files that are missing sidecar files.

    Args:
        root: Directory to search.
        extensions: File extensions to check (default: common doc types).

    Returns:
        List of document paths missing .ocr.txt and/or .meta.json.
    """
    if extensions is None:
        extensions = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".pdf", ".docx", ".xlsx"}

    missing = []
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in extensions:
            continue
        # Skip sidecar files themselves
        if f.suffix in {".txt", ".json"}:
            continue

        ocr_path = f.with_suffix(f.suffix + ".ocr.txt") if f.suffix else f.parent / f"{f.name}.ocr.txt"
        meta_path = f.parent / f"{f.stem}.meta.json"

        # Actually check the standard sidecar naming: {stem}.ocr.txt, {stem}.meta.json
        # (not {full_name}.ocr.txt)
        ocr_path = f.parent / f"{f.stem}.ocr.txt"
        meta_path = f.parent / f"{f.stem}.meta.json"

        if not ocr_path.exists() or not meta_path.exists():
            missing.append(f)

    return sorted(missing)


def sidecar_status(root: Path) -> dict:
    """Check sidecar coverage for a directory tree.

    Returns dict with counts of documents with/without sidecars.
    """
    extensions = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".pdf", ".docx", ".xlsx"}
    total = 0
    has_both = 0
    has_ocr = 0
    has_meta = 0
    has_neither = 0

    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in extensions:
            continue

        total += 1
        ocr_path = f.parent / f"{f.stem}.ocr.txt"
        meta_path = f.parent / f"{f.stem}.meta.json"

        o = ocr_path.exists()
        m = meta_path.exists()

        if o and m:
            has_both += 1
        elif o:
            has_ocr += 1
        elif m:
            has_meta += 1
        else:
            has_neither += 1

    return {
        "total": total,
        "has_both": has_both,
        "has_ocr_only": has_ocr,
        "has_meta_only": has_meta,
        "has_neither": has_neither,
        "coverage_pct": round(has_both / total * 100, 1) if total else 0,
    }


# ── CLI ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sidecar generator — create .ocr.txt and .meta.json files for documents",
    )
    subparsers = parser.add_subparsers(dest="command", help="Sub-command")

    # create
    create_parser = subparsers.add_parser("create", help="Create sidecars for a single file")
    create_parser.add_argument("file", type=Path, help="Path to the document file")
    create_parser.add_argument("--ocr-text", default=None, help="OCR text content (or - to read from stdin)")
    create_parser.add_argument("--meta", default=None, help="JSON metadata string")
    create_parser.add_argument("--meta-file", default=None, help="Path to JSON metadata file")
    create_parser.add_argument("--overwrite", action="store_true", help="Overwrite existing sidecars")

    # status
    status_parser = subparsers.add_parser("status", help="Check sidecar coverage for a directory")
    status_parser.add_argument("directory", type=Path, help="Directory to check")
    status_parser.add_argument("--verbose", "-v", action="store_true", help="Show missing files")

    # backfill
    backfill_parser = subparsers.add_parser("backfill", help="List documents missing sidecars")
    backfill_parser.add_argument("directory", type=Path, help="Directory to scan")
    backfill_parser.add_argument("--extensions", nargs="+", default=None, help="File extensions to check")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "create":
        file_path = args.file
        if not file_path.exists():
            print(f"❌ File not found: {file_path}")
            sys.exit(1)

        ocr_text = args.ocr_text
        if ocr_text == "-":
            ocr_text = sys.stdin.read()

        meta = None
        if args.meta:
            meta = json.loads(args.meta)
        elif args.meta_file:
            meta = json.loads(Path(args.meta_file).read_text(encoding="utf-8"))

        created = create_sidecars(file_path, ocr_text=ocr_text, meta=meta, overwrite=args.overwrite)
        for p in created:
            print(f"  ✅ Created: {p}")

    elif args.command == "status":
        directory = args.directory
        if not directory.is_dir():
            print(f"❌ Not a directory: {directory}")
            sys.exit(1)

        status = sidecar_status(directory)
        print(f"\n📊 Sidecar Coverage: {directory}")
        print(f"   Total documents:    {status['total']}")
        print(f"   Both sidecars:      {status['has_both']}")
        print(f"   OCR only:           {status['has_ocr_only']}")
        print(f"   Meta only:          {status['has_meta_only']}")
        print(f"   Neither:            {status['has_neither']}")
        print(f"   Coverage:            {status['coverage_pct']}%")

        if args.verbose and status["has_neither"] > 0:
            missing = find_documents_without_sidecars(directory)
            print(f"\n   Missing sidecars:")
            for f in missing[:50]:
                print(f"     {f.relative_to(directory)}")

    elif args.command == "backfill":
        directory = args.directory
        exts = set(args.extensions) if args.extensions else None
        missing = find_documents_without_sidecars(directory, extensions=exts)
        if not missing:
            print("✅ All documents have sidecars.")
        else:
            print(f"\n📋 {len(missing)} document(s) missing sidecars:")
            for f in missing:
                print(f"   {f}")


if __name__ == "__main__":
    main()