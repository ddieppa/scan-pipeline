from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class OrganizationRule:
    id: str
    names: tuple[str, ...]
    category: str
    person: str
    destination: str = ""
    folder_alias: str = ""


@dataclass(frozen=True)
class DocumentTypeRule:
    id: str
    patterns: tuple[str, ...]
    filename_template: str
    labels: dict[str, str]


@dataclass(frozen=True)
class CompiledRules:
    default_destination: str
    current_year: int
    organizations: tuple[OrganizationRule, ...]
    document_types: tuple[DocumentTypeRule, ...]
    people_aliases: dict[str, tuple[str, ...]]
    routing: dict[str, Any]
    org_regexes: dict[str, re.Pattern[str]]
    document_type_regexes: dict[str, re.Pattern[str]]


def _pattern_to_regex(pattern: str) -> str:
    """Convert a scan rule pattern to a regex fragment.

    Patterns containing \\b, \\d, \\s (as literal backslash sequences in the YAML)
    are treated as raw regex. Short patterns (<=3 chars, likely acronyms like
    'ssn', 'rx') get word boundaries added to prevent substring false matches.
    Everything else is escaped for literal matching.
    """
    # Check if the pattern contains regex word boundary or digit markers
    # In YAML, \\b is stored as the two-char sequence backslash+b in the string
    if '\\b' in pattern or '\\d' in pattern or '\\s' in pattern:
        return pattern
    # Short patterns (acronyms) need word boundaries to avoid false matches
    # e.g., "ssn" should not match "nervousness", "rx" should not match "parx"
    if len(pattern) <= 3:
        return r'\b' + re.escape(pattern) + r'\b'
    return re.escape(pattern)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_yaml_config(path: Path) -> dict[str, Any]:
    return _load_yaml(path)


def load_compiled_rules(path: Path) -> CompiledRules:
    raw = _load_yaml(path)
    organizations = tuple(
        OrganizationRule(
            id=entry["id"],
            names=tuple(entry["names"]),
            category=entry["category"],
            person=entry["person"],
            destination=entry.get("destination", ""),
            folder_alias=entry.get("folder_alias", ""),
        )
        for entry in raw.get("organizations", [])
    )
    document_types = tuple(
        DocumentTypeRule(
            id=doc_type_id,
            patterns=tuple(config.get("patterns", [])),
            filename_template=config["filename_template"],
            labels=config.get("labels", {}),
        )
        for doc_type_id, config in raw.get("document_types", {}).items()
    )
    org_regexes = {
        org.id: re.compile("|".join(r'\b' + re.escape(name.lower()) + r'\b' for name in org.names))
        for org in organizations
    }
    type_regexes = {
        rule.id: re.compile("|".join(_pattern_to_regex(pattern.lower()) for pattern in rule.patterns))
        for rule in document_types
    }
    people_aliases = {
        person: tuple(alias.lower() for alias in aliases)
        for person, aliases in raw.get("people", {}).get("aliases", {}).items()
    }
    # Include category_routing, filename_heuristics, and path_person_detection in routing dict
    routing = raw.get("routing", {})
    routing["category_routing"] = raw.get("category_routing", {})
    routing["filename_heuristics"] = raw.get("filename_heuristics", {})
    routing["path_person_detection"] = raw.get("filename_heuristics", {}).get("path_person_detection", {})
    return CompiledRules(
        default_destination=raw.get("default_destination", "03-Resources/Scans/"),
        current_year=int(raw.get("current_year", 2026)),
        organizations=organizations,
        document_types=document_types,
        people_aliases=people_aliases,
        routing=routing,
        org_regexes=org_regexes,
        document_type_regexes=type_regexes,
    )