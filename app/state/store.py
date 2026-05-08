from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


class StateStore:
    def __init__(self, state_dir: Path, suggestion_path: Path) -> None:
        self.state_dir = state_dir
        self.suggestion_path = suggestion_path
        self.batches_path = state_dir / "batches.json"
        self.proposals_path = state_dir / "proposals.json"
        self.feedback_path = state_dir / "feedback.jsonl"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def save_batch(self, batch_id: str, payload: dict[str, Any]) -> None:
        data = self._load_json(self.batches_path, {})
        data[batch_id] = payload
        self._write_json(self.batches_path, data)

    def save_proposal(self, sha256: str, payload: dict[str, Any]) -> None:
        data = self._load_json(self.proposals_path, {})
        data[sha256] = payload
        self._write_json(self.proposals_path, data)

    def get_proposal(self, sha256: str) -> dict[str, Any] | None:
        return self._load_json(self.proposals_path, {}).get(sha256)

    def record_feedback(self, feedback: dict[str, Any]) -> None:
        payload = dict(feedback)
        payload.setdefault("timestamp", datetime.now().isoformat(timespec="seconds"))
        with self.feedback_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._update_suggestions()

    def load_feedback(self) -> list[dict[str, Any]]:
        if not self.feedback_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with self.feedback_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries

    def _update_suggestions(self) -> None:
        feedback_entries = self.load_feedback()
        overrides = Counter(
            (
                entry.get("type"),
                entry.get("ruleMatchId"),
                entry.get("fromDest"),
                entry.get("toDest"),
            )
            for entry in feedback_entries
            if entry.get("type") == "override_destination" and entry.get("fromDest") and entry.get("toDest")
        )
        suggestions = []
        for (feedback_type, rule_match_id, from_dest, to_dest), count in overrides.items():
            if count < 2:
                continue
            suggestions.append(
                {
                    "kind": feedback_type,
                    "ruleMatchId": rule_match_id,
                    "fromDest": from_dest,
                    "toDest": to_dest,
                    "supportCount": count,
                    "status": "pending_approval",
                }
            )
        payload = {
            "version": 1,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "suggestions": suggestions,
        }
        with self.suggestion_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=True)

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
