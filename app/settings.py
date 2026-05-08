from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _default_root() -> Path:
    return Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    pipeline_root: Path
    inbox_root: Path
    qsync_root: Path
    state_dir: Path
    config_dir: Path
    max_workers: int
    webhook_url: str | None
    webhook_secret: str | None
    webhook_session_key: str

    @property
    def scan_rules_path(self) -> Path:
        return self.config_dir / "scan_rules.yaml"

    @property
    def file_types_path(self) -> Path:
        return self.config_dir / "file_types.yaml"

    @property
    def notifications_path(self) -> Path:
        return self.config_dir / "notifications.yaml"

    @property
    def rule_suggestions_path(self) -> Path:
        return self.config_dir / "rule_suggestions.yaml"


def load_settings() -> Settings:
    pipeline_root = Path(os.environ.get("SCAN_PIPELINE_ROOT", _default_root())).resolve()
    config_dir = pipeline_root / "config"
    state_dir = Path(os.environ.get("SCAN_STATE_DIR", pipeline_root / "state-data")).resolve()
    inbox_root = Path(os.environ.get("SCAN_INBOX", "/home/ddieppa/scanner/inbox")).resolve()
    qsync_root = Path(os.environ.get("SCAN_QSYNC_ROOT", "/mnt/e/QSync")).resolve()
    max_workers = int(os.environ.get("SCAN_MAX_WORKERS", "4"))
    return Settings(
        pipeline_root=pipeline_root,
        inbox_root=inbox_root,
        qsync_root=qsync_root,
        state_dir=state_dir,
        config_dir=config_dir,
        max_workers=max_workers,
        webhook_url=os.environ.get("OPENCLAW_WEBHOOK_URL"),
        webhook_secret=os.environ.get("OPENCLAW_WEBHOOK_SECRET"),
        webhook_session_key=os.environ.get("OPENCLAW_SESSION_KEY", "agent:main:scan-ingest"),
    )
