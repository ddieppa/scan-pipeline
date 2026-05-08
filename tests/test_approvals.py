from __future__ import annotations

import unittest
from pathlib import Path

from app.coordinator import approve_proposal, deny_proposal
from app.settings import Settings
from app.state.store import StateStore


def _settings(tmp_path: Path) -> Settings:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    suggestion_path = config_dir / "rule_suggestions.yaml"
    suggestion_path.write_text("version: 1\ngenerated_at: null\nsuggestions: []\n", encoding="utf-8")
    return Settings(
        pipeline_root=tmp_path,
        inbox_root=tmp_path / "inbox",
        qsync_root=tmp_path / "qsync",
        state_dir=tmp_path / "state",
        config_dir=config_dir,
        max_workers=2,
        webhook_url=None,
        webhook_secret=None,
        webhook_session_key="agent:main:scan-ingest",
    )


class ApprovalTests(unittest.TestCase):
    def test_approve_moves_file(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            settings = _settings(tmp_path)
            source = tmp_path / "inbox" / "scan.pdf"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"pdf")
            store = StateStore(settings.state_dir, settings.rule_suggestions_path)
            store.save_proposal(
                "abc",
                {
                    "path": str(source),
                    "proposal": {
                        "proposedName": "approved.pdf",
                        "proposedDest": "02-Areas/Test/",
                        "ruleMatchId": "bill:test",
                    },
                    "status": "pending",
                },
            )

            result = approve_proposal(settings, "abc")
            self.assertTrue(result["ok"])
            self.assertTrue((settings.qsync_root / "02-Areas/Test/approved.pdf").exists())
            self.assertFalse(source.exists())

    def test_deny_records_reason(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            settings = _settings(tmp_path)
            source = tmp_path / "inbox" / "scan.pdf"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"pdf")
            store = StateStore(settings.state_dir, settings.rule_suggestions_path)
            store.save_proposal(
                "abc",
                {
                    "path": str(source),
                    "proposal": {
                        "proposedName": "approved.pdf",
                        "proposedDest": "02-Areas/Test/",
                        "ruleMatchId": "bill:test",
                    },
                    "status": "pending",
                },
            )

            result = deny_proposal(settings, "abc", "wrong folder")
            self.assertTrue(result["ok"])
            updated = store.get_proposal("abc")
            self.assertEqual(updated["status"], "denied")
            self.assertEqual(updated["denyReason"], "wrong folder")


if __name__ == "__main__":
    unittest.main()
