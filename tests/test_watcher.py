from __future__ import annotations

import unittest
from pathlib import Path

from app.settings import Settings
from app.watcher.bridge import StableFileBatcher


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        pipeline_root=tmp_path,
        inbox_root=tmp_path,
        qsync_root=tmp_path / "qsync",
        state_dir=tmp_path / "state",
        config_dir=tmp_path / "config",
        max_workers=2,
        webhook_url="http://127.0.0.1:18789/hooks/agent",
        webhook_secret="secret",
        webhook_session_key="agent:main:scan-ingest",
    )


class WatcherTests(unittest.TestCase):
    def test_stable_file_batcher_ignores_unsupported_files(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            calls: list[tuple[str, str, dict]] = []

            def fake_sender(url: str, secret: str, payload: dict) -> dict:
                calls.append((url, secret, payload))
                return {"ok": True}

            batcher = StableFileBatcher(
                settings=_settings(tmp_path),
                debounce_seconds=0.0,
                batch_window_seconds=0.0,
                stable_poll_interval_seconds=0.0,
                stable_checks=1,
                sender=fake_sender,
            )
            unsupported = tmp_path / "note.txt"
            unsupported.write_text("skip", encoding="utf-8")
            batcher.queue(unsupported)
            batcher.flush()
            self.assertEqual(calls, [])

    def test_stable_file_batcher_posts_supported_file(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            calls: list[tuple[str, str, dict]] = []

            def fake_sender(url: str, secret: str, payload: dict) -> dict:
                calls.append((url, secret, payload))
                return {"ok": True}

            batcher = StableFileBatcher(
                settings=_settings(tmp_path),
                debounce_seconds=0.0,
                batch_window_seconds=0.0,
                stable_poll_interval_seconds=0.0,
                stable_checks=1,
                sender=fake_sender,
            )
            supported = tmp_path / "scan.pdf"
            supported.write_bytes(b"%PDF-1.7 sample")
            batcher.queue(supported)
            batcher.flush()
            self.assertEqual(len(calls), 1)
            self.assertTrue("message" in calls[0][2] or "action" in calls[0][2])


if __name__ == "__main__":
    unittest.main()
