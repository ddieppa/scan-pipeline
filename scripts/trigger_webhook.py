#!/usr/bin/env python3
"""Trigger OpenClaw webhook for scan batch.

Usage:
    python trigger_webhook.py --files file1.jpg file2.pdf
"""

import argparse
import json
import urllib.request
from pathlib import Path
from uuid import uuid4


def trigger_webhook(files: list[str], webhook_url: str, secret: str):
    batch_id = f"batch-{uuid4().hex[:12]}"
    
    payload = {
        "action": "create_flow",
        "goal": f"[SCAN v3] Process new files: {json.dumps(files)}",
        "status": "queued",
        "notifyPolicy": "done_only",
        "metadata": {
            "batchId": batch_id,
            "files": files,
            "source": "scan-pipeline-v3",
        }
    }
    
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST"
    )
    request.add_header("Authorization", f"Bearer {secret}")
    request.add_header("Content-Type", "application/json")
    
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8")
        print(f"Webhook response: {body}")
        return json.loads(body) if body else {"ok": True}


def parse_args():
    parser = argparse.ArgumentParser(description="Trigger scan webhook")
    parser.add_argument("--files", nargs="+", required=True, help="Files to process")
    parser.add_argument("--webhook-url", default="http://localhost:18789/plugins/webhooks/scan-ingest")
    parser.add_argument("--secret", default="ba3lKWfG9vFH8FCqGRqQ2MK0w5mQC3UtuMQuPo4TkJs")
    return parser.parse_args()


def main():
    args = parse_args()
    result = trigger_webhook(args.files, args.webhook_url, args.secret)
    print(f"Triggered: {result}")


if __name__ == "__main__":
    main()
