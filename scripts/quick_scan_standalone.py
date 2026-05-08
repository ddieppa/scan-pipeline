#!/usr/bin/env python3
"""Quick inbox scan - standalone script with full OCR."""
import json
import sys
import time
from pathlib import Path
from datetime import datetime

# Setup paths
ROOT = Path("/home/ddieppa/.openclaw/workspace/scan-pipeline-v3")
sys.path.insert(0, str(ROOT))

from app.classify.config import load_compiled_rules
from app.classify.engine import classify_document
from app.extractors.pdf import extract_pdf
from app.extractors.images import extract_image
from app.extractors.docx import extract_docx
from app.extractors.xlsx import extract_xlsx
from app.utils import scan_date_from_mtime

# Load rules + config
rules = load_compiled_rules(ROOT / "config" / "scan_rules.yaml")
import yaml
file_type_config = yaml.safe_load((ROOT / "config" / "file_types.yaml").read_text()) or {}

PDF_INSPECT_PAGES = int(file_type_config.get("pdf", {}).get("inspect_pages", 3))
PDF_MIN_TEXT = int(file_type_config.get("pdf", {}).get("min_text_chars_before_skip_ocr", 20))
PDF_RENDER_DPI = int(file_type_config.get("pdf", {}).get("render_dpi", 200))
IMAGE_MAX_OCR = int(file_type_config.get("images", {}).get("max_ocr_chars", 20000))
SUPPORTED = {e.lower() for e in file_type_config.get("supported_extensions", [])}

# Scan inbox
inbox = Path("/mnt/e/Qsync-Scanned-Documents/!!!Check/")
files = [f for f in inbox.rglob("*") if f.is_file() and f.suffix.lower() in SUPPORTED]

print(f"📁 Found {len(files)} files in inbox\n")
print("=" * 70)

results = []
for i, f in enumerate(files, 1):
    t0 = time.time()
    scan_date = scan_date_from_mtime(f)

    # ── Extract text (with OCR) ──
    try:
        if f.suffix.lower() == ".pdf":
            extraction = extract_pdf(f, PDF_INSPECT_PAGES, PDF_MIN_TEXT, PDF_RENDER_DPI)
        elif f.suffix.lower() in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}:
            extraction = extract_image(f, IMAGE_MAX_OCR)
        elif f.suffix.lower() == ".docx":
            extraction = extract_docx(f, 400, 200)
        elif f.suffix.lower() == ".xlsx":
            extraction = extract_xlsx(f, 8, 300)
        else:
            continue
        text = extraction.text
        text_source = extraction.text_source
    except Exception as exc:
        text = f.name.replace("_", " ").replace("-", " ")
        text_source = "error_fallback"

    # ── Classify ──
    result = classify_document(text, f, scan_date, rules)
    elapsed = time.time() - t0

    print(f"\n{i}. 📄 {f.name}")
    print(f"   📂 Parent: {f.parent.name}")
    print(f"   🔍 Source: {text_source} ({elapsed:.1f}s)")
    print(f"   🏷️  Type: {result.doc_type}")
    print(f"   👤 Person: {result.person}")
    print(f"   🎯 Confidence: {result.confidence:.0%}")
    print(f"   💾 Proposed: {result.proposed_name}")
    print(f"   📁 Destination: {result.proposed_dest}")
    if result.ambiguous:
        print(f"   ⚠️  Ambiguous: {result.ambiguous}")
    if result.question:
        print(f"   ❓ Question: {result.question}")

    results.append({
        "file": str(f),
        "name": f.name,
        "type": result.doc_type,
        "person": result.person,
        "confidence": result.confidence,
        "proposed_name": result.proposed_name,
        "destination": result.proposed_dest,
        "text_source": text_source,
        "elapsed_s": round(elapsed, 1),
        "ambiguous": result.ambiguous,
        "question": result.question,
    })

# Summary
print(f"\n{'='*70}")
print(f"📊 SUMMARY: {len(results)} files processed")
high_conf = [r for r in results if r["confidence"] >= 0.70]
med_conf = [r for r in results if 0.50 <= r["confidence"] < 0.70]
low_conf = [r for r in results if r["confidence"] < 0.50]
ambiguous = [r for r in results if r["ambiguous"]]

print(f"   🟢 High confidence (≥70%): {len(high_conf)}")
print(f"   🟡 Medium confidence (50-69%): {len(med_conf)}")
print(f"   🔴 Low confidence (<50%): {len(low_conf)}")
print(f"   ❓ Ambiguous: {len(ambiguous)}")

total_time = sum(r["elapsed_s"] for r in results)
print(f"   ⏱️  Total time: {total_time:.1f}s (avg {total_time/max(len(results),1):.1f}s/file)")

if ambiguous:
    print(f"\n❓ AMBIGUOUS ITEMS NEEDING INPUT:")
    for r in ambiguous:
        print(f"   • {r['name']} → {r['ambiguous']}")
        print(f"     Question: {r['question']}")

# Save results
output = ROOT / "state-data" / "last_scan_results.json"
output.parent.mkdir(exist_ok=True)
with open(output, "w") as fh:
    json.dump(results, fh, indent=2)
print(f"\n💾 Results saved to: {output}")