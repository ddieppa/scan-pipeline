#!/usr/bin/env python3
"""Quick inbox scan - standalone script with tabular output + OCR/PDF extraction."""
import json
import sys
from pathlib import Path
from datetime import datetime

# Setup paths
ROOT = Path("/home/ddieppa/.openclaw/workspace/scan-pipeline-v3")
sys.path.insert(0, str(ROOT))

from app.classify.config import load_compiled_rules
from app.classify.engine import classify_document
from app.extractors.common import ocr_image_file, extract_pdf_text

# Load rules
rules = load_compiled_rules(ROOT / "config" / "scan_rules.yaml")

# Scan inbox
inbox = Path("/mnt/e/Qsync-Scanned-Documents/!!!Check/")
files = [f for f in inbox.rglob("*") if f.is_file()]

print(f"📁 Found {len(files)} files in inbox\n")
print("🔍 Running OCR/PDF extraction for better suggestions...")
print("=" * 200)

# Table header
print(f"{'#':<4} {'Original Folder':<45} {'Original File':<40} {'Suggested New Name':<50} {'Suggested Folder':<40} {'Conf':<8} {'Method'}")
print("=" * 200)

results = []
for i, f in enumerate(files, 1):
    scan_date = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
    
    # Extract text based on file type
    ext = f.suffix.lower()
    extracted_text = ""
    extraction_method = "filename"
    
    if ext == ".pdf":
        try:
            pdf_result = extract_pdf_text(f)
            extracted_text = pdf_result.text
            extraction_method = "PDF"
        except Exception:
            extracted_text = ""
    elif ext in [".jpg", ".jpeg", ".png", ".tif", ".tiff"]:
        try:
            extracted_text = ocr_image_file(f, timeout_seconds=20)
            if extracted_text.strip():
                extraction_method = "OCR"
        except Exception:
            extracted_text = ""
    
    # Combine filename + extracted text for classification
    # Give priority to extracted text if available
    if extracted_text.strip():
        text = f.name.replace("_", " ").replace("-", " ") + " " + extracted_text[:500]
    else:
        text = f.name.replace("_", " ").replace("-", " ")
    
    result = classify_document(text, f, scan_date, rules)
    
    # Create mutable copy for corrections
    from dataclasses import replace
    
    # Apply manual corrections based on analysis
    # Row 1-2: Vehicle registration for 1983 GLBK Mobile Home
    if "Registration Renewal" in str(f.parent) and "Scan2020-01-22" in f.name:
        result = replace(result, 
            proposed_name=f"2020-01-22_VehicleRegistration_Natalie_1983_GLBK_MobileHome_{f.stem.split('_')[-1]}.jpg",
            proposed_dest="04-Archives/Vehicles/Registration/2020/",
            confidence=0.95
        )
    
    # Row 3-21: Bella's MiniMe homework - include MiniMe in name
    elif "MiniMe" in str(f.parent):
        result = replace(result,
            proposed_name=f"2020-07-11_Education_MiniMe_Isabella_{f.stem.split('_')[-1]}.jpg",
            proposed_dest="02-Areas/Family/Isabella/Education/MiniMe/",
            confidence=0.72
        )
    
    # Row 22-23: Bella's daycare party photos
    elif "Daycare Party" in str(f.parent):
        result = replace(result,
            proposed_name=f"2018-11-28_CreativeWork_DaycareParty_Isabella_{f.stem.split('_')[-1]}.jpg",
            proposed_dest="03-Resources/Scans/",
            confidence=0.72
        )
    
    # Row 24: IRS EIN - better name
    elif f.name == "IRS EIN.pdf":
        result = replace(result,
            proposed_name="2022-02-07_NAndD_Tek_Solutions_EIN_88-0528126_Daniel.pdf",
            proposed_dest="02-Areas/Business/NAndD Tek Solutions/IRS/",
            confidence=0.95
        )
    
    # Row 25: S-Corp confirmation
    elif f.name == "s-corp confirmation.pdf":
        result = replace(result,
            proposed_name="2022-07-18_NAndD_Tek_Solutions_SCorp_Confirmation_Daniel.pdf",
            proposed_dest="02-Areas/Business/NAndD Tek Solutions/IRS/",
            confidence=0.95
        )
    
    # Row 26: Form 2553
    elif f.name == "small corp form 2553.pdf":
        result = replace(result,
            proposed_name="2022-02-09_NAndD_Tek_Solutions_Form2553_Daniel.pdf",
            proposed_dest="02-Areas/Business/NAndD Tek Solutions/IRS/",
            confidence=0.95
        )
    
    # Get original folder (relative to inbox)
    original_folder = str(f.parent.relative_to(inbox)) if f.parent != inbox else "(root)"
    
    # Do NOT truncate - show full names
    orig_folder = original_folder
    orig_file = f.name
    new_name = result.proposed_name
    new_folder = result.proposed_dest
    
    # Format confidence (multiply by 100 if it's stored as decimal 0.72 instead of 72)
    conf_val = result.confidence if result.confidence > 1 else result.confidence * 100
    conf = f"{conf_val:.0f}%"
    
    print(f"{i:<4} {orig_folder:<45} {orig_file:<40} {new_name:<50} {new_folder:<40} {conf:<8} {extraction_method}")
    
    results.append({
        "id": i,
        "original_folder": original_folder,
        "original_name": f.name,
        "proposed_name": result.proposed_name,
        "proposed_dest": result.proposed_dest,
        "confidence": result.confidence,
        "file_path": str(f),
        "extraction_method": extraction_method,
        "ocr_text_preview": extracted_text[:200] if extracted_text else "",
    })

print("=" * 200)
print(f"\n📊 Total: {len(results)} files")

# Show breakdown
ocr_count = sum(1 for r in results if r["extraction_method"] == "OCR")
pdf_count = sum(1 for r in results if r["extraction_method"] == "PDF")
filename_count = sum(1 for r in results if r["extraction_method"] == "filename")
print(f"   🔍 OCR extracted: {ocr_count} | 📄 PDF extracted: {pdf_count} | 📝 Filename only: {filename_count}")

# Save results
output = ROOT / "state-data" / "last_scan_results.json"
output.parent.mkdir(exist_ok=True)
with open(output, "w") as f:
    json.dump(results, f, indent=2)
print(f"💾 Results saved to: {output}")
print(f"\n💡 Use the row numbers to approve/decline/suggest changes")
