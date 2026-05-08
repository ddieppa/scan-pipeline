"""LLM fallback for low-confidence document classification.

When the regex-based classifier returns low confidence (< 0.70) or an ambiguous
result, this module calls a configured LLM to get a second opinion on the
document type, person, and provider.

Default flow: try local LLM first (60s timeout), fall back to cloud on failure.
Sensitive doc types (identity_card, medical_record, prescription, lab_requisition)
use local only — if local fails, they go to manual review instead of cloud.

Results are cached in the llm_classifications SQLite table keyed by
ocr_text_sha256 + model. Every call is logged (both local and cloud).
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
            source TEXT DEFAULT 'unknown',
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
                latency_ms: int | None, source: str = "unknown") -> None:
    """Save LLM classification result to cache."""
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO llm_classifications
               (ocr_text_hash, model, doc_type, person, provider,
                confidence, reasoning, latency_ms, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (ocr_text_sha, model, doc_type, person, provider,
             confidence, reasoning, latency_ms, source),
        )
        conn.commit()
    finally:
        conn.close()


def _log_feedback(ocr_text_sha: str, model: str, doc_type_proposed: str | None,
                  latency_ms: int, source: str, fallback: bool = False) -> None:
    """Log every LLM call (local and cloud) to feedback.jsonl for audit."""
    entry = {
        "ts": datetime.now().isoformat(),
        "sha256": ocr_text_sha,
        "model": model,
        "source": source,  # "local" or "cloud"
        "doc_type_proposed": doc_type_proposed,
        "latency_ms": latency_ms,
        "fallback": fallback,  # True if this was a fallback from local→cloud
    }
    FEEDBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(FEEDBACK_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _call_ollama(endpoint: str, model: str, prompt: str,
                 timeout_seconds: int, max_retries: int) -> tuple[str | None, str | None]:
    """Call an Ollama-compatible API endpoint synchronously.

    Returns (response_text, error_message) tuple.
    response_text is None on failure.
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
                return data.get("message", {}).get("content", ""), None
        except (URLError, HTTPError, TimeoutError, OSError, json.JSONDecodeError) as e:
            last_error = str(e)
            logger.debug(f"LLM call attempt {attempt + 1} failed: {e}")
            continue

    logger.warning(f"LLM call failed after {1 + max_retries} attempt(s): {last_error}")
    return None, last_error


def _parse_llm_response(raw: str) -> dict | None:
    """Parse JSON from LLM response text."""
    if not raw:
        return None

    import re
    # Try to find JSON in code fences
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

    Flow:
    1. For sensitive doc types: try local only. If local fails → manual review (no cloud).
    2. For non-sensitive docs: try local first (60s timeout). If local fails/times out → fall back to cloud.
    3. Results are cached by (ocr_text_hash, model).

    Returns:
        Dict with keys: doc_type, person, provider, confidence, reasoning,
        model, from_cache, source, fallback. Or None if LLM fallback is disabled or fails.
    """
    if config is None:
        config = load_llm_config()

    fallback_cfg = config.get("llm_fallback", {})

    if not fallback_cfg.get("enabled", False):
        return None

    ocr_text_sha = _ocr_text_hash(ocr_text)
    cloud_cfg = fallback_cfg.get("cloud", {})
    local_cfg = fallback_cfg.get("local", {})
    sensitive_types = set(fallback_cfg.get("sensitive_doc_types_skip_cloud", []))
    is_sensitive = sensitive_doc_type in sensitive_types if sensitive_doc_type else False

    # Build prompt (shared between local and cloud)
    truncated_text = ocr_text[:2000] if len(ocr_text) > 2000 else ocr_text
    doc_types_str = ", ".join(known_doc_types) if known_doc_types else "lab_requisition, medical_record, prescription, bill, identity_card, insurance, business, vehicle, employment, receipt, dental"
    people_str = ", ".join(known_people) if known_people else "Daniel, Natalie, Isabella"

    prompt = (
        f"Classify this scanned document. OCR text:\n{truncated_text}\n\n"
        f"Known doc types: {doc_types_str}\n"
        f"Known people: {people_str}\n\n"
        f'Respond in JSON only: {{"doc_type": "...", "person": "...", "provider": "...", "confidence": 0.0-1.0, "reasoning": "..."}}'
    )

    # ── Step 1: Try local LLM first (for all docs, if enabled) ──
    local_result = None
    if local_cfg.get("enabled", False):
        local_model = local_cfg.get("model", "qwen3-vl:8b")
        local_endpoint = local_cfg.get("endpoint", "http://localhost:11434/api/chat")
        local_timeout = local_cfg.get("timeout_seconds", 60)
        local_retries = local_cfg.get("max_retries", 0)

        # Check cache first
        cached = _check_cache(ocr_text_sha, local_model)
        if cached and cached.get("doc_type"):
            cached["from_cache"] = True
            cached["model"] = local_model
            cached["source"] = "local"
            cached["fallback"] = False
            logger.info(f"LLM cache hit (local): {local_model} → {cached['doc_type']} for {ocr_text_sha[:12]}...")
            return cached

        # Call local LLM
        logger.info(f"🖥️  LLM LOCAL attempt: {local_model} (timeout: {local_timeout}s) for {ocr_text_sha[:12]}...")
        t0 = time.time()
        raw_response, error = _call_ollama(local_endpoint, local_model, prompt, local_timeout, local_retries)
        latency_ms = int((time.time() - t0) * 1000)

        if raw_response:
            parsed = _parse_llm_response(raw_response)
            if parsed and parsed.get("doc_type"):
                local_result = {
                    "doc_type": parsed.get("doc_type", "").strip(),
                    "person": parsed.get("person", "").strip(),
                    "provider": parsed.get("provider", "").strip(),
                    "confidence": max(0.0, min(1.0, float(parsed.get("confidence", 0.0)))),
                    "reasoning": parsed.get("reasoning", "").strip(),
                    "model": local_model,
                    "from_cache": False,
                    "latency_ms": latency_ms,
                    "source": "local",
                    "fallback": False,
                }
                _save_cache(ocr_text_sha, local_model, local_result["doc_type"],
                             local_result["person"], local_result["provider"],
                             local_result["confidence"], local_result["reasoning"],
                             latency_ms, source="local")
                _log_feedback(ocr_text_sha, local_model, local_result["doc_type"],
                              latency_ms, source="local", fallback=False)
                logger.info(f"✅ LLM LOCAL success: {local_model} → {local_result['doc_type']}/{local_result['person']} ({latency_ms}ms)")
            else:
                logger.warning(f"⚠️  LLM LOCAL returned unparseable response ({latency_ms}ms)")
                _log_feedback(ocr_text_sha, local_model, None, latency_ms, source="local", fallback=False)
        else:
            logger.warning(f"⚠️  LLM LOCAL failed: {error} ({latency_ms}ms)")
            _log_feedback(ocr_text_sha, local_model, None, latency_ms, source="local", fallback=False)

    # If we got a local result, return it
    if local_result:
        if is_sensitive:
            logger.info(f"🔒 Sensitive doc '{sensitive_doc_type}' — using local result only (no cloud)")
        return local_result

    # ── Step 2: If local failed or disabled, try cloud ──
    # For sensitive docs: NO cloud fallback → return None (manual review)
    if is_sensitive:
        logger.info(f"🔒 Sensitive doc '{sensitive_doc_type}' — local failed, skipping cloud → manual review")
        return None

    if not cloud_cfg.get("enabled", True):
        logger.info("Cloud LLM disabled, no fallback available")
        return None

    cloud_model = cloud_cfg.get("model", "glm-5.1:cloud")
    cloud_endpoint = cloud_cfg.get("endpoint", "http://localhost:11434/api/chat")
    cloud_timeout = cloud_cfg.get("timeout_seconds", 0) or None  # 0 or None = no timeout
    cloud_retries = cloud_cfg.get("max_retries", 1)
    did_fallback = local_result is None and local_cfg.get("enabled", False)

    # Check cache first
    cached = _check_cache(ocr_text_sha, cloud_model)
    if cached and cached.get("doc_type"):
        cached["from_cache"] = True
        cached["model"] = cloud_model
        cached["source"] = "cloud"
        cached["fallback"] = did_fallback
        logger.info(f"LLM cache hit (cloud): {cloud_model} → {cached['doc_type']} for {ocr_text_sha[:12]}...")
        return cached

    # Call cloud LLM
    fallback_label = " (FALLBACK from local)" if did_fallback else ""
    logger.info(f"☁️  LLM CLOUD attempt: {cloud_model}{fallback_label} for {ocr_text_sha[:12]}...")
    t0 = time.time()
    raw_response, error = _call_ollama(cloud_endpoint, cloud_model, prompt, cloud_timeout, cloud_retries)
    latency_ms = int((time.time() - t0) * 1000)

    if not raw_response:
        logger.warning(f"⚠️  LLM CLOUD failed: {error} ({latency_ms}ms)")
        _log_feedback(ocr_text_sha, cloud_model, None, latency_ms, source="cloud", fallback=did_fallback)
        return None

    parsed = _parse_llm_response(raw_response)
    if not parsed or not parsed.get("doc_type"):
        logger.warning(f"⚠️  LLM CLOUD returned unparseable response ({latency_ms}ms)")
        _log_feedback(ocr_text_sha, cloud_model, None, latency_ms, source="cloud", fallback=did_fallback)
        return None

    cloud_result = {
        "doc_type": parsed.get("doc_type", "").strip(),
        "person": parsed.get("person", "").strip(),
        "provider": parsed.get("provider", "").strip(),
        "confidence": max(0.0, min(1.0, float(parsed.get("confidence", 0.0)))),
        "reasoning": parsed.get("reasoning", "").strip(),
        "model": cloud_model,
        "from_cache": False,
        "latency_ms": latency_ms,
        "source": "cloud",
        "fallback": did_fallback,
    }
    _save_cache(ocr_text_sha, cloud_model, cloud_result["doc_type"],
                cloud_result["person"], cloud_result["provider"],
                cloud_result["confidence"], cloud_result["reasoning"],
                latency_ms, source="cloud")
    _log_feedback(ocr_text_sha, cloud_model, cloud_result["doc_type"],
                  latency_ms, source="cloud", fallback=did_fallback)

    fallback_msg = " (FALLBACK from local)" if did_fallback else ""
    logger.info(f"✅ LLM CLOUD success: {cloud_model} → {cloud_result['doc_type']}/{cloud_result['person']} ({latency_ms}ms){fallback_msg}")

    return cloud_result


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

        # Cloud vs local split
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

        # Timeout/fallback rate
        c.execute("""
            SELECT COUNT(*) FROM llm_classifications
            WHERE latency_ms > 30000
        """)
        stats["likely_timeouts"] = c.fetchone()[0]

        # Fallback rate from feedback.jsonl
        fallback_count = 0
        if FEEDBACK_LOG.exists():
            try:
                lines = FEEDBACK_LOG.read_text(encoding="utf-8").strip().split("\n")
                stats["feedback_log_entries"] = len([l for l in lines if l.strip()])
                for line in lines:
                    try:
                        entry = json.loads(line)
                        if entry.get("fallback"):
                            fallback_count += 1
                    except (json.JSONDecodeError, TypeError):
                        pass
            except Exception:
                stats["feedback_log_entries"] = 0
        else:
            stats["feedback_log_entries"] = 0
        stats["fallback_count"] = fallback_count

        return stats
    finally:
        conn.close()