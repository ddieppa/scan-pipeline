#!/usr/bin/env python3
"""Batch backfill sidecars for existing document files.

Usage:
    python3 batch_backfill.py /path/to/documents/directory [--limit N] [--dry-run]

Scans a directory tree, finds documents without sidecars, and creates them.
For image files: runs OCR via the local OCR skill.
For PDF files: creates stub OCR with metadata from filename.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# OCR script path
OCR_SCRIPT = Path("/home/ddieppa/.openclaw/workspace/skills/ocr-local/scripts/ocr.js")


def parse_filename_metadata(filename: str) -> dict:
    """Extract metadata from a PARA-format filename.
    
    Format: YYYY-MM-DD_Provider_Description_Person_NNN.ext
    """
    stem = Path(filename).stem
    parts = stem.split("_")
    
    meta = {
        "date": "",
        "provider": "",
        "description": "",
        "patient": "",
        "doc_type": "",
        "ocr_date": datetime.now().strftime("%Y-%m-%d"),
    }
    
    if len(parts) >= 3:
        # Date
        if len(parts[0]) == 10 and parts[0][4] == "-" and parts[0][7] == "-":
            meta["date"] = parts[0]
        elif len(parts[0]) == 4 and parts[0].isdigit():
            meta["date"] = parts[0]  # year only
        
        # Provider (usually parts[1])
        meta["provider"] = parts[1] if len(parts) > 1 else ""
        
        # Patient (usually the last meaningful part before _NNN)
        # Look for known person names
        person_keywords = ["Daniel", "Natalie", "Isabella", "Nala", "Grisell", "Nany", "Bella"]
        for kw in person_keywords:
            if kw in parts:
                meta["patient"] = kw
                break
        
        # Doc type inference from keywords in filename
        type_keywords = {
            "BillingStatement": ["BillingStatement", "Billing", "Statement"],
            "DischargeSummary": ["DischargeSummary", "Discharge"],
            "EyeExam": ["EyeExam", "Eye_Exam"],
            "EyePrescription": ["EyePrescription", "Prescription"],
            "LabReport": ["Lab", "UrineCulture", "ChemPanel", "CBC"],
            "Ultrasound": ["Ultrasound"],
            "Xray": ["Xray", "X-ray"],
            "HospitalRecord": ["Hospital", "EmergencyCare", "ER", "Ingreso"],
            "Prescription": ["Rx", "Prescription", "Meds"],
            "Insurance": ["Insurance", "Authorization", "Claim"],
            "Surgery": ["Surgery", "PostOp"],
            "MedicalRecord": ["MedicalRecords", "MedicalRecord"],
            "VisitSummary": ["VisitSummary"],
            "LabRequisition": ["LabRequisition"],
        }
        
        stem_lower = stem.lower()
        for dtype, keywords in type_keywords.items():
            if any(kw.lower() in stem_lower for kw in keywords):
                meta["doc_type"] = dtype
                break
        
        # Description from remaining parts
        desc_parts = []
        for i, p in enumerate(parts[2:], 2):
            if p in person_keywords or (len(p) == 3 and p.isdigit()):
                continue
            desc_parts.append(p)
        if desc_parts:
            meta["description"] = " ".join(desc_parts)
    
    return meta


def run_ocr(image_path: Path) -> str:
    """Run OCR on an image file using the local OCR skill."""
    try:
        result = subprocess.run(
            ["node", str(OCR_SCRIPT), str(image_path), "--lang", "eng"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout if result.returncode == 0 else f"[OCR failed: {result.stderr[:200]}]"
    except subprocess.TimeoutExpired:
        return "[OCR timeout]"
    except Exception as e:
        return f"[OCR error: {e}]"


def create_sidecar_for_file(file_path: Path, overwrite: bool = False) -> bool:
    """Create sidecars for a single file. Returns True if created."""
    stem = file_path.stem
    parent = file_path.parent
    
    ocr_path = parent / f"{stem}.ocr.txt"
    meta_path = parent / f"{stem}.meta.json"
    
    # Skip if both exist and not overwriting
    if not overwrite and ocr_path.exists() and meta_path.exists():
        return False
    
    # Extract metadata from filename
    meta = parse_filename_metadata(file_path.name)
    
    # OCR
    if file_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}:
        ocr_text = run_ocr(file_path)
    elif file_path.suffix.lower() == ".pdf":
        ocr_text = f"[PDF file - full OCR deferred]\nFilename: {file_path.name}\n"
    else:
        ocr_text = f"[Unsupported file type: {file_path.suffix}]\n"
    
    # Write OCR text
    if overwrite or not ocr_path.exists():
        ocr_path.write_text(ocr_text + "\n", encoding="utf-8")
    
    # Build metadata
    full_meta = {
        **meta,
        "source_file": file_path.name,
    }
    
    # Write metadata
    if overwrite or not meta_path.exists():
        meta_path.write_text(
            json.dumps(full_meta, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    
    return True


def main():
    parser = argparse.ArgumentParser(description="Batch backfill sidecars")
    parser.add_argument("directory", type=Path, help="Root directory to scan")
    parser.add_argument("--limit", "-l", type=int, default=None, help="Max files to process")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing sidecars")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show each file processed")
    args = parser.parse_args()
    
    root = args.directory
    if not root.is_dir():
        print(f"❌ Not a directory: {root}")
        sys.exit(1)
    
    # Find all document files
    extensions = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".pdf", ".docx", ".xlsx"}
    files = []
    for f in root.rglob("*"):
        if f.is_file() and f.suffix.lower() in extensions:
            # Skip sidecar files
            if f.suffix in {".txt", ".json"}:
                continue
            files.append(f)
    
    files = sorted(files)
    
    # Filter to files missing sidecars (unless overwrite)
    if not args.overwrite:
        needs_sidecar = []
        for f in files:
            ocr_path = f.parent / f"{f.stem}.ocr.txt"
            meta_path = f.parent / f"{f.stem}.meta.json"
            if not ocr_path.exists() or not meta_path.exists():
                needs_sidecar.append(f)
        files = needs_sidecar
    
    if args.limit:
        files = files[:args.limit]
    
    print(f"📋 Found {len(files)} file(s) to process in {root}")
    
    if args.dry_run:
        for f in files:
            print(f"  [DRY-RUN] {f.relative_to(root)}")
        return
    
    created = 0
    skipped = 0
    errors = 0
    
    for i, f in enumerate(files, 1):
        try:
            if create_sidecar_for_file(f, overwrite=args.overwrite):
                created += 1
                if args.verbose:
                    print(f"  ✅ [{i}/{len(files)}] {f.relative_to(root)}")
                else:
                    print(f"  ✅ [{i}/{len(files)}] {f.name}")
            else:
                skipped += 1
        except Exception as e:
            errors += 1
            print(f"  ❌ [{i}/{len(files)}] {f.name}: {e}")
    
    print(f"\n📊 Done: Created {created} | Skipped {skipped} | Errors {errors}")


if __name__ == "__main__":
    main()