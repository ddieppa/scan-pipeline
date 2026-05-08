# Scan Pipeline v3

This is a clean-room replacement for `workspace/scan-pipeline`. It is designed for OpenClaw-first operation and does not depend on cron as the primary trigger.

## What It Does

- Watches a scan inbox locally with `watchdog`
- Waits until new files are stable
- Sends a batched authenticated webhook call into OpenClaw
- Runs one Python batch scan task per webhook-triggered batch
- Extracts text from PDFs, images, `.docx`, and `.xlsx`
- Classifies documents from config-driven rules
- Builds a run-scoped duplicate index once per batch
- Stores proposals and approval feedback
- Applies file moves and renames only after explicit approval

## OpenClaw Hook Setup

1. Point OpenClaw at the intended workspace via `agents.defaults.workspace`.
2. Enable the Webhooks plugin on the Gateway.
3. Add a dedicated route similar to:

```json
{
  "plugins": {
    "entries": {
      "webhooks": {
        "enabled": true,
        "config": {
          "routes": {
            "scan-ingest": {
              "path": "/plugins/webhooks/scan-ingest",
              "sessionKey": "agent:main:scan-ingest",
              "secret": {
                "source": "env",
                "provider": "default",
                "id": "OPENCLAW_SCAN_WEBHOOK_SECRET"
              }
            }
          }
        }
      }
    }
  }
}
```

4. Start the watcher with `python scripts/run_watcher.py`.
5. The watcher batches stable arrivals and POSTs an authenticated request to the webhook route.
6. OpenClaw then runs `python scripts/run_batch.py --files ...` in the scan-ingest session or a wrapper around it.

## Environment

- `SCAN_PIPELINE_ROOT`: override the pipeline root directory
- `SCAN_INBOX`: source inbox path
- `SCAN_QSYNC_ROOT`: destination root for approved files
- `SCAN_STATE_DIR`: proposal and feedback storage directory
- `SCAN_MAX_WORKERS`: local processing parallelism
- `OPENCLAW_WEBHOOK_URL`: full webhook URL
- `OPENCLAW_WEBHOOK_SECRET`: shared secret for the route
- `OPENCLAW_SESSION_KEY`: route session key for documentation and metadata

## Approval Flow

- Batches and proposals are persisted under the state directory.
- Use:
  - `python scripts/approve.py <sha256> [--dest ...] [--name ...]`
  - `python scripts/deny.py <sha256> --reason "..."`
- Overrides and corrections are logged as feedback.
- Repeated feedback is converted into entries in `config/rule_suggestions.yaml`.

## Notes

- Active rules are loaded from `config/scan_rules.yaml`.
- Suggestions are written only to `config/rule_suggestions.yaml`.
- Active rules are never modified automatically.
