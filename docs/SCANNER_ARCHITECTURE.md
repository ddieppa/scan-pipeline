# Scanner Ingestion Pipeline — Architecture & Reference

## Architecture

```
Windows Scanner App
    ↓ saves to
\\wsl.localhost\Ubuntu\home\ddieppa\scanner\inbox
    = /home/ddieppa/scanner/inbox  (native ext4, inotify works)
    ↓ inotifywait detects close_write / moved_to
    ↓ 10s debounce + size stabilization check
    ↓ flock prevents concurrent runs
    ↓ move to processing/
/home/ddieppa/scanner/processing/
    ↓ triggers openclaw cron run
    ↓ (scan inbox processor job)
OpenClaw main session
    ↓ runs scan_workflow.py scan
    ↓ OCR + classify + propose
    ↓ Telegram DM to Daniel (8277191343)
    ↓ Daniel approves/denies in chat
    ↓ approved files → E:\QSync\... (PARA destination)
    ↓ denied files → manual review
```

**Safety net:** Cron job at 2pm ET daily checks all three inboxes (new, processing, legacy).

## Directory Structure

```
/home/ddieppa/scanner/
├── inbox/          ← Windows scanner writes here
├── processing/     ← files staged for OpenClaw pipeline
├── archive/        ← (future) processed files
├── error/          ← (future) failed files
├── logs/           ← watcher logs
│   ├── watcher.log
│   ├── watcher-stdout.log
│   ├── watcher-stderr.log
│   └── fallback.log
└── scripts/
    └── watch-inbox.sh   ← the watcher daemon
```

## Service Management

```bash
# Check status
systemctl --user status scan-watcher.service

# View logs
tail -f /home/ddieppa/scanner/logs/watcher.log
journalctl --user -u scan-watcher -f

# Restart
systemctl --user restart scan-watcher.service

# Stop / Start
systemctl --user stop scan-watcher.service
systemctl --user start scan-watcher.service

# Manual trigger (if watcher is down)
openclaw cron run bde66a5f-cc54-4c5a-aad3-9dc04625910d
```

## Key Configuration

| Setting | Value | File/Location |
|---|---|---|
| Inbox path | `/home/ddieppa/scanner/inbox` | `scan-pipeline-v3/app/settings.py` |
| Windows path | `\\wsl.localhost\Ubuntu\home\ddieppa\scanner\inbox` | Scanner app config |
| QSync root | `/mnt/e/QSync` | `scan-pipeline-v3/app/settings.py` |
| Allowed extensions | pdf, jpg, jpeg, png, tif, tiff, bmp, gif, webp | `watch-inbox.sh` |
| Settle time | 10s | `watch-inbox.sh` |
| Cooldown | 30s | `watch-inbox.sh` |
| Cron job ID | `bde66a5f-cc54-4c5a-aad3-9dc04625910d` | OpenClaw cron |
| Cron schedule | Daily 2pm ET | OpenClaw cron |
| Telegram chat ID | `8277191343` | Cron job delivery config |

## Troubleshooting

| Problem | Check | Fix |
|---|---|---|
| Watcher not running | `systemctl --user status scan-watcher` | `systemctl --user restart scan-watcher` |
| Files not detected | Check inotifywait is in the process list | `which inotifywait`; reinstall `inotify-tools` |
| WSL not accessible from Windows | Open `\\wsl.localhost\Ubuntu\home\ddieppa\scanner\inbox` in Explorer | Restart WSL: `wsl --shutdown` |
| Pipeline not triggered | Check watcher.log for "OpenClaw scan job triggered" | `openclaw cron run bde66a5f...` manually |
| Telegram not delivering | Check cron job delivery config has `to: 8277191343` | Update via `openclaw cron edit` |

## Recovery Procedures

### Watcher crashed
```bash
systemctl --user restart scan-watcher.service
```

### WSL restarted
The systemd service should auto-start. Verify:
```bash
systemctl --user status scan-watcher.service
```

### Missed files (manual reconcile)
```bash
# Check all inboxes
cd /home/ddieppa/.openclaw/workspace/scan-pipeline-v3
SCAN_INBOX=/home/ddieppa/scanner/inbox python3 scan_workflow.py scan
SCAN_INBOX=/home/ddieppa/scanner/processing python3 scan_workflow.py scan
```

### Clean stuck processing files
```bash
# Move stale files back to inbox for reprocessing
mv /home/ddieppa/scanner/processing/* /home/ddieppa/scanner/inbox/
```

## Backup Recommendations

- **Watcher script:** `/home/ddieppa/scanner/scripts/watch-inbox.sh` (in WSL, backed up by git nightly)
- **Service file:** `~/.config/systemd/user/scan-watcher.service` (small, can recreate)
- **Scan pipeline:** `/home/ddieppa/.openclaw/workspace/scan-pipeline-v3/` (git tracked)
- **Cron jobs:** Stored in `~/.openclaw/cron/jobs.json` (auto-backed up by git)
- **Scan state:** `~/.openclaw/workspace/scan-pipeline-v3/state-data/` (proposal history)

## Component Versions

| Component | Version | Purpose |
|---|---|---|
| inotify-tools | 3.22.6.0 | Filesystem event monitoring |
| scan_workflow.py | v3 | OCR + classification + proposal |
| watch-inbox.sh | 1.0 | inotify watcher daemon |
| systemd service | 1.0 | Auto-start/restart watcher |