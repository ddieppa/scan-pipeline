"""Classification corrections / learning module.

Stores corrections in SQLite and applies them before classification
to improve future classifications based on past user overrides.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from app.classify.config import CompiledRules

DB_PATH = Path(__file__).resolve().parent.parent.parent / "state-data" / "scan_history.db"


def _get_conn() -> sqlite3.Connection:
    """Get a connection to the scan history DB, ensuring the corrections table exists."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS classification_corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_doc_type TEXT NOT NULL,
            original_person TEXT NOT NULL,
            original_provider TEXT NOT NULL,
            original_confidence REAL NOT NULL,
            corrected_doc_type TEXT NOT NULL,
            corrected_person TEXT NOT NULL,
            corrected_provider TEXT NOT NULL,
            org_id TEXT,
            correction_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sample_keywords TEXT  -- JSON array of keywords from OCR text
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_corrections_org_type
        ON classification_corrections(org_id, corrected_doc_type)
    """)
    conn.commit()
    return conn


def extract_keywords(text: str, max_keywords: int = 20) -> list[str]:
    """Extract key meaningful words from OCR text for correction matching.

    Filters out common stop words and short tokens, returning the most
    distinctive keywords from the text.
    """
    if not text:
        return []

    # Common English stop words + OCR noise
    stop_words = {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will", "would",
        "could", "should", "may", "might", "can", "this", "that", "these",
        "those", "it", "its", "not", "no", "nor", "as", "if", "then", "than",
        "too", "very", "just", "about", "above", "after", "again", "all",
        "also", "am", "any", "because", "before", "between", "both", "each",
        "few", "more", "most", "other", "our", "out", "over", "own", "same",
        "she", "he", "they", "them", "their", "there", "these", "those",
        "through", "under", "until", "up", "we", "what", "when", "where",
        "which", "while", "who", "whom", "why", "you", "your", "1", "2", "3",
        "4", "5", "6", "7", "8", "9", "0", "00", "01", "10", "20", "30",
        "date", "time", "name", "address", "phone", "number", "page", "please",
        "see", "print", "yes", "true", "false", "none", "null", "void",
    }

    # Normalize and tokenize
    words = re.findall(r"[a-z]{3,}", text.lower())
    # Filter stop words and deduplicate while preserving order
    seen = set()
    keywords = []
    for w in words:
        if w not in stop_words and w not in seen:
            seen.add(w)
            keywords.append(w)
            if len(keywords) >= max_keywords:
                break
    return keywords


def save_correction(
    original_doc_type: str,
    original_person: str,
    original_provider: str,
    original_confidence: float,
    corrected_doc_type: str,
    corrected_person: str,
    corrected_provider: str,
    org_id: str | None = None,
    ocr_text: str | None = None,
) -> int:
    """Save a classification correction to the database.

    If ocr_text is provided, keywords are extracted automatically.
    Returns the correction ID.
    """
    conn = _get_conn()
    keywords = extract_keywords(ocr_text) if ocr_text else []
    try:
        cursor = conn.execute(
            """INSERT INTO classification_corrections
               (original_doc_type, original_person, original_provider,
                original_confidence, corrected_doc_type, corrected_person,
                corrected_provider, org_id, sample_keywords)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                original_doc_type, original_person, original_provider,
                original_confidence, corrected_doc_type, corrected_person,
                corrected_provider, org_id, json.dumps(keywords),
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def check_corrections(
    org_id: str | None,
    ocr_text: str | None,
    rules: CompiledRules,
) -> dict[str, Any] | None:
    """Check if any corrections match the current document context.

    Matches by org_id and keyword overlap. Returns the best matching
    correction as a dict with corrected values and a confidence boost,
    or None if no match found.
    """
    conn = _get_conn()
    try:
        # Get all corrections, ordered by most recent first
        rows = conn.execute(
            "SELECT * FROM classification_corrections ORDER BY correction_date DESC LIMIT 100"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return None

    current_keywords = set(extract_keywords(ocr_text)) if ocr_text else set()

    best_match = None
    best_overlap = 0
    min_overlap = 3  # Need at least 3 keyword matches

    for row in rows:
        row_dict = dict(row)
        # Check org_id match
        correction_org_id = row_dict.get("org_id")
        if org_id and correction_org_id and org_id != correction_org_id:
            continue

        # Check keyword overlap
        sample_keywords_raw = row_dict.get("sample_keywords", "[]")
        if isinstance(sample_keywords_raw, str):
            try:
                sample_keywords = set(json.loads(sample_keywords_raw))
            except (json.JSONDecodeError, TypeError):
                sample_keywords = set()
        else:
            sample_keywords = set()

        if not sample_keywords:
            continue

        overlap = len(current_keywords & sample_keywords)
        if overlap >= min_overlap and overlap > best_overlap:
            best_overlap = overlap
            best_match = row_dict

    if best_match:
        # Calculate confidence boost based on keyword overlap
        overlap_ratio = best_overlap / max(len(best_match.get("sample_keywords", "[]")), 1) if best_match else 0
        confidence_boost = min(0.10, 0.05 + overlap_ratio * 0.05)

        return {
            "corrected_doc_type": best_match["corrected_doc_type"],
            "corrected_person": best_match["corrected_person"],
            "corrected_provider": best_match["corrected_provider"],
            "confidence_boost": confidence_boost,
            "correction_id": best_match["id"],
        }

    return None


def list_corrections(limit: int = 20) -> list[dict]:
    """List recent classification corrections."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM classification_corrections ORDER BY correction_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def format_corrections(corrections: list[dict]) -> str:
    """Format corrections for CLI display."""
    if not corrections:
        return "📭 No corrections found."

    lines = [f"📋 **{len(corrections)} recent correction(s):**\n"]
    lines.append(f"{'ID':<5} {'Date':<20} {'Original Type':<18} {'→ Corrected Type':<18} {'Org':<20}")
    lines.append("─" * 85)

    for c in corrections:
        date = str(c.get("correction_date", ""))[:19]
        lines.append(
            f"{c['id']:<5} {date:<20} "
            f"{c['original_doc_type'][:17]:<18} → {c['corrected_doc_type'][:17]:<18} "
            f"{(c.get('org_id') or '')[:19]:<20}"
        )

    return "\n".join(lines)