"""LLM fallback for low-confidence document classification.

When the regex-based classifier returns low confidence (< 0.70) or an ambiguous
result, this module calls a configured LLM (cloud by default, local opt-in)
to get a second opinion on the document type, person, and provider.

Results are cached in the llm_classifications SQLite table keyed by
ocr_text_sha256 + model, so the same OCR text never calls the LLM twice.
Every cloud call is logged to feedback.jsonl for audit.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import yaml

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DB_PATH = BASE_DIR / "state-data" / "scan_history.db"
LLM_CONFIG_PATH = BASE_DIR / "config" / "llm.yaml"
FEEDBACK_LOG = BASE_DIR / "state-data" / "feedback.jsonl"


def load_llm_config() -> dict:
    """Load LLM fallback config from config/llm.yaml."""
    if LLM_CONFIG_PATH.exists():
        with open(LLM_CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _get_conn() -> sqlite3.Connection:
    """Get a connection with the llm_classifications table ensured."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_classifications (
            ocr_text_hash TEXT NOT NULL,
            model TEXT NOT NULL,
            doc_type TEXT,
            person TEXT,
            provider TEXT,
            confidence REAL,
            reasoning TEXT,
            latency_ms INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (ocr_text_hash, model)
        )
    """)
    conn.commit()
    return conn


def _ocr_text_hash(ocr_text: str) -> str:
    """SHA256 hash of OCR text for cache key."""
    return hashlib.sha256(ocr_text.encode("utf-8", errors="replace")).hexdigest()


def _check_cache(ocr_text_sha: str, model: str) -> dict | None:
    """Check if we already have an LLM classification cached for this text+model."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM llm_classifications WHERE ocr_text_hash = ? AND model = ?",
            (ocr_text_sha, model),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _save_cache(ocr_text_sha: str, model: str, doc_type: str | None,
                person: str | None, provider: str | None,
                confidence: float | None, reasoning: str | None,
                latency_ms: int | None) -> None:
    """Save LLM classification result to cache."""
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO llm_classifications
               (ocr_text_hash, model, doc_type, person, provider,
                confidence, reasoning, latency_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (ocr_text_sha, model, doc_type, person, provider,
             confidence, reasoning, latency_ms),
        )
        conn.commit()
    finally:
        conn.close()


def _log_feedback(ocr_text_sha: str, model: str, doc_type_proposed: str | None,
                  latency_ms: int) -> None:
    """Log every cloud LLM call to feedback.jsonl for audit."""
    entry = {
        "ts": datetime.now().isoformat(),
        "sha256": ocr_text_sha,
        "model": model,
        "doc_type_proposed": doc_type_proposed,
        "latency_ms": latency_ms,
    }
    FEEDBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(FEEDBACK_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _call_ollama(endpoint: str, model: str, prompt: str,
                 timeout_seconds: int, max_retries: int) -> str | None:
    """Call an Ollama-compatible API endpoint synchronously.

    POST to endpoint with {"model": ..., "messages": [...], "stream": false}.
    Returns the response content text, or None on failure.
    """
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode("utf-8")

    last_error = None
    for attempt in range(1 + max_retries):
        try:
            req = Request(
                endpoint,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urlopen(req, timeout=timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("message", {}).get("content", "")
        except (URLError, HTTPError, TimeoutError, OSError, json.JSONDecodeError) as e:
            last_error = e
            logger.debug(f"LLM call attempt {attempt + 1} failed: {e}")
            continue

    logger.warning(f"LLM call failed after {1 + max_retries} attempt(s): {last_error}")
    return None


def _parse_llm_response(raw: str) -> dict | None:
    """Parse JSON from LLM response text.

    The LLM is asked to respond with:
    {"doc_type": ..., "person": ..., "provider": ..., "confidence": ..., "reasoning": ...}

    Tries to extract JSON from markdown code fences or raw text.
    Returns parsed dict or None.
    """
    if not raw:
        return None

    # Try to find JSON in code fences
    import re
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
    if fence_match:
        raw = fence_match.group(1).strip()

    # Try direct JSON parse
    try:
        result = json.loads(raw)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Try to find first { ... } block
    brace_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if brace_match:
        try:
            result = json.loads(brace_match.group(0))
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    return None


def run_llm_fallback(
    ocr_text: str,
    candidates: list[dict] | None = None,
    config: dict | None = None,
    known_doc_types: list[str] | None = None,
    known_people: list[str] | None = None,
    sensitive_doc_type: str | None = None,
) -> dict | None:
    """Run LLM fallback classification for a low-confidence document.

    Args:
        ocr_text: Full OCR text of the document.
        candidates: List of candidate classifications with confidence scores.
        config: LLM config dict (loaded from config/llm.yaml if not provided).
        known_doc_types: List of valid doc type names for the prompt.
        known_people: List of known person names for the prompt.
        sensitive_doc_type: If the doc was classified as a sensitive type
            (e.g., identity_card), routing logic changes.

    Returns:
        Dict with keys: doc_type, person, provider, confidence, reasoning,
        model, from_cache. Or None if LLM fallback is disabled or fails.
    """
    if config is None:
        config = load_llm_config()

    fallback_cfg = config.get("llm_fallback", {})

    if not fallback_cfg.get("enabled", False):
        return None

    trigger = fallback_cfg.get("trigger", {})
    # Note: trigger condition checking is done by the caller (engine.py)
    # This function is only called when trigger conditions are already met.

    ocr_text_sha = _ocr_text_hash(ocr_text)
    cloud_cfg = fallback_cfg.get("cloud", {})
    local_cfg = fallback_cfg.get("local", {})
    sensitive_types = set(fallback_cfg.get("sensitive_doc_types_skip_cloud", []))

    # Determine which model to use
    is_sensitive = sensitive_doc_type in sensitive_types if sensitive_doc_type else False

    if is_sensitive:
        if not local_cfg.get("enabled", False):
            # Sensitive doc type, local not enabled → skip entirely
            logger.info(f"Skipping LLM fallback for sensitive doc type '{sensitive_doc_type}' (local LLM not enabled)")
            return None
        # Sensitive doc type, local enabled → use local only, never cloud
        model = local_cfg.get("model", "qwen3-vl:8b")
        endpoint = local_cfg.get("endpoint", "http://localhost:11434/api/chat")
        timeout = local_cfg.get("timeout_seconds", 5)
        max_retries = local_cfg.get("max_retries", 0)
    else:
        # Default: use cloud model
        if not cloud_cfg.get("enabled", True):
            return None
        model = cloud_cfg.get("model", "glm-5.1:cloud")
        endpoint = cloud_cfg.get("endpoint", "http://localhost:11434/api/chat")
        timeout = cloud_cfg.get("timeout_seconds", 30)
        max_retries = cloud_cfg.get("max_retries", 1)

    # Check cache first
    cached = _check_cache(ocr_text_sha, model)
    if cached and cached.get("doc_type"):
        cached["from_cache"] = True
        cached["model"] = model
        return cached

    # Build prompt
    truncated_text = ocr_text[:2000] if len(ocr_text) > 2000 else ocr_text
    doc_types_str = ", ".join(known_doc_types) if known_doc_types else "lab_requisition, medical_record, prescription, bill, identity_card, insurance, business, vehicle, employment, receipt, dental"
    people_str = ", ".join(known_people) if known_people else "Daniel, Natalie, Isabella"

    prompt = (
        f"Classify this scanned document. OCR text:\n{truncated_text}\n\n"
        f"Known doc types: {doc_types_str}\n"
        f"Known people: {people_str}\n\n"
        f'Respond in JSON only: {{"doc_type": "...", "person": "...", "provider": "...", "confidence": 0.0-1.0, "reasoning": "..."}}'
    )

    # Call LLM
    t0 = time.time()
    raw_response = _call_ollama(endpoint, model, prompt, timeout, max_retries)
    latency_ms = int((time.time() - t0) * 1000)

    if not raw_response:
        logger.warning("LLM returned empty response")
        return None

    # Parse response
    parsed = _parse_llm_response(raw_response)
    if not parsed:
        logger.warning(f"Could not parse LLM response as JSON: {raw_response[:200]}")
        return None

    # Validate and extract fields
    doc_type = parsed.get("doc_type", "").strip()
    person = parsed.get("person", "").strip()
    provider = parsed.get("provider", "").strip()
    confidence = parsed.get("confidence", 0.0)
    reasoning = parsed.get("reasoning", "").strip()

    # Clamp confidence
    try:
        confidence = float(confidence)
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.0

    if not doc_type:
        return None

    # Save to cache
    _save_cache(ocr_text_sha, model, doc_type, person, provider,
                confidence, reasoning, latency_ms)

    # Log cloud calls to feedback.jsonl
    if not is_sensitive:  # Only log cloud calls
        _log_feedback(ocr_text_sha, model, doc_type, latency_ms)

    return {
        "doc_type": doc_type,
        "person": person,
        "provider": provider,
        "confidence": confidence,
        "reasoning": reasoning,
        "model": model,
        "from_cache": False,
        "latency_ms": latency_ms,
    }


def get_llm_stats() -> dict:
    """Get LLM fallback usage statistics from the llm_classifications table."""
    conn = _get_conn()
    try:
        c = conn.cursor()

        stats: dict[str, Any] = {}

        # Total LLM calls
        c.execute("SELECT COUNT(*) FROM llm_classifications")
        stats["total_calls"] = c.fetchone()[0]

        # Per-model stats
        c.execute("""
            SELECT model, COUNT(*) as cnt,
                   AVG(latency_ms) as avg_latency,
                   MIN(latency_ms) as min_latency,
                   MAX(latency_ms) as max_latency
            FROM llm_classifications
            GROUP BY model
        """)
        model_stats = []
        for row in c.fetchall():
            model_stats.append({
                "model": row[0],
                "count": row[1],
                "avg_latency_ms": round(row[2] or 0, 1),
                "min_latency_ms": row[3],
                "max_latency_ms": row[4],
            })
        stats["per_model"] = model_stats

        # Cloud vs local split (heuristic: models containing "cloud" → cloud)
        c.execute("""
            SELECT
                SUM(CASE WHEN model LIKE '%cloud%' THEN 1 ELSE 0 END) as cloud_count,
                SUM(CASE WHEN model NOT LIKE '%cloud%' THEN 1 ELSE 0 END) as local_count
            FROM llm_classifications
        """)
        row = c.fetchone()
        stats["cloud_calls"] = row[0] or 0
        stats["local_calls"] = row[1] or 0

        # Average latency overall
        c.execute("SELECT AVG(latency_ms) FROM llm_classifications")
        avg = c.fetchone()[0]
        stats["avg_latency_ms"] = round(avg, 1) if avg else 0

        # Timeout/fallback rate: entries with very high latency (> timeout threshold)
        c.execute("""
            SELECT COUNT(*) FROM llm_classifications
            WHERE latency_ms > 30000
        """)
        stats["likely_timeouts"] = c.fetchone()[0]

        # Feedback log stats
        if FEEDBACK_LOG.exists():
            try:
                lines = FEEDBACK_LOG.read_text(encoding="utf-8").strip().split("\n")
                stats["feedback_log_entries"] = len([l for l in lines if l.strip()])
            except Exception:
                stats["feedback_log_entries"] = 0
        else:
            stats["feedback_log_entries"] = 0

        return stats
    finally:
        conn.close()