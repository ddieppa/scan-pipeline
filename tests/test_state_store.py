from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from app.state.store import StateStore


class StateStoreTests(unittest.TestCase):
    def test_rule_suggestions_require_repeated_feedback(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            suggestion_path = tmp_path / "rule_suggestions.yaml"
            store = StateStore(tmp_path / "state", suggestion_path)
            store.record_feedback(
                {
                    "type": "override_destination",
                    "ruleMatchId": "bill:test",
                    "fromDest": "03-Resources/Scans/",
                    "toDest": "02-Areas/Family/Test/",
                }
            )
            first_payload = yaml.safe_load(suggestion_path.read_text(encoding="utf-8"))
            self.assertEqual(first_payload["suggestions"], [])

            store.record_feedback(
                {
                    "type": "override_destination",
                    "ruleMatchId": "bill:test",
                    "fromDest": "03-Resources/Scans/",
                    "toDest": "02-Areas/Family/Test/",
                }
            )
            second_payload = yaml.safe_load(suggestion_path.read_text(encoding="utf-8"))
            self.assertEqual(len(second_payload["suggestions"]), 1)
            self.assertEqual(second_payload["suggestions"][0]["status"], "pending_approval")


if __name__ == "__main__":
    unittest.main()
