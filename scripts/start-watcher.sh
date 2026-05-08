#!/bin/bash
# Start the scan-pipeline-v3 file watcher

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V3_DIR="$(dirname "$SCRIPT_DIR")"

# Environment
export SCAN_PIPELINE_ROOT="$V3_DIR"
export SCAN_INBOX="/mnt/e/Qsync-Scanned-Documents/!!!Check"
export SCAN_QSYNC_ROOT="/mnt/e/QSync"
export SCAN_STATE_DIR="$V3_DIR/state-data"
export SCAN_MAX_WORKERS="4"
export OPENCLAW_WEBHOOK_URL="http://localhost:18789/plugins/webhooks/scan-ingest"
export OPENCLAW_WEBHOOK_SECRET="ba3lKWfG9vFH8FCqGRqQ2MK0w5mQC3UtuMQuPo4TkJs"
export OPENCLAW_SESSION_KEY="agent:main:scan-ingest"

# Ensure state directory exists
mkdir -p "$SCAN_STATE_DIR"

cd "$V3_DIR"
python3 scripts/run_watcher.py
