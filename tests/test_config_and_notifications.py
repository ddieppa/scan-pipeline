from __future__ import annotations

import unittest
from pathlib import Path

from app.classify.config import load_compiled_rules, load_yaml_config
from app.notifications.render import render_notification


ROOT = Path(__file__).resolve().parents[1]


class ConfigAndNotificationTests(unittest.TestCase):
    def test_rules_load(self) -> None:
        rules = load_compiled_rules(ROOT / "config" / "scan_rules.yaml")
        self.assertEqual(rules.default_destination, "03-Resources/Scans/")
        self.assertTrue(any(org.id == "nicklaus-childrens" for org in rules.organizations))

    def test_notification_groups_results(self) -> None:
        settings = load_yaml_config(ROOT / "config" / "notifications.yaml")
        body = render_notification(
            "batch-123",
            [
                {
                    "status": "success",
                    "proposedDest": "02-Areas/Test/",
                    "proposedName": "file1.pdf",
                    "confidence": 0.91,
                    "ruleMatchId": "bill:test",
                    "contentDuplicates": [],
                    "duplicatesAnywhere": [],
                },
                {"status": "error", "filename": "broken.pdf", "error": "boom"},
            ],
            settings,
        )
        self.assertIn("Batch: `batch-123`", body)
        self.assertIn("file1.pdf", body)
        self.assertIn("broken.pdf", body)


if __name__ == "__main__":
    unittest.main()
