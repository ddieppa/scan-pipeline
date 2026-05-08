#!/usr/bin/env python3
"""Quick scan of all inbox files - standalone version."""
import sys
import os
from pathlib import Path

# Add the scan-pipeline-v3 root to path
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from app.pipeline import process_batch
from app.settings import load_settings

inbox = Path('/mnt/e/Qsync-Scanned-Documents/!!!Check/')
files = [f for f in inbox.rglob('*') if f.is_file()]

print(f"Found {len(files)} files in inbox")

settings = load_settings()
result = process_batch(settings, files)

# Print summary
print(f"\n{'='*60}")
print(f"Batch ID: {result.get('batch_id')}")
print(f"Status: {result.get('status')}")
print(f"Total files: {result.get('total_files')}")
print(f"Processed: {result.get('processed')}")
print(f"Proposed: {result.get('proposed')}")

for r in result.get('results', []):
    print(f"\n📄 {r['original_name']}")
    print(f"   → {r['proposed_name']}")
    print(f"   → {r['proposed_destination']}")
    print(f"   Confidence: {r['confidence']}%")
