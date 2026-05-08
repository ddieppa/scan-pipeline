#!/usr/bin/env python3
"""Run a batch scan when triggered by webhook.

Usage:
    python scripts/run_batch.py --files file1.pdf file2.jpg ...
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pipeline import process_batch
from app.settings import load_settings


def parse_args():
    parser = argparse.ArgumentParser(description="Process a batch of scan files")
    parser.add_argument("--files", nargs="+", required=True, help="Files to process")
    parser.add_argument("--batch-id", default=None, help="Batch ID")
    return parser.parse_args()


def main():
    args = parse_args()
    settings = load_settings()
    
    files = [Path(f) for f in args.files]
    result = process_batch(settings, files, batch_id=args.batch_id)
    
    # Print JSON output for webhook consumption
    print(json.dumps(result, indent=2, default=str))
    
    return 0 if result.get("status") == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
