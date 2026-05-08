#!/usr/bin/env python3
"""Run the file watcher for scan-pipeline-v3.

Watches the inbox folder and stages new files for batch processing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.settings import load_settings
from app.watcher.bridge import run_watcher
from app.classify.config import load_yaml_config


def main():
    settings = load_settings()
    print(f"Starting watcher for inbox: {settings.inbox_root}")
    print(f"QSync root: {settings.qsync_root}")
    print(f"State dir: {settings.state_dir}")
    
    # Ensure state directory exists
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    
    file_types = load_yaml_config(settings.file_types_path)
    run_watcher(settings, file_types)


if __name__ == "__main__":
    main()
