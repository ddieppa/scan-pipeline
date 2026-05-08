"""OCR text quality assessment module.

Evaluates the quality of extracted text to detect poor OCR results
that may benefit from re-processing with a different extraction method.
"""
from __future__ import annotations

import re


def assess_text_quality(text: str) -> float:
    """Assess the quality of OCR-extracted text.

    Returns a quality score from 0.0 (garbage) to 1.0 (excellent).

    Checks:
    - Ratio of printable characters vs total (skip noise lines)
    - Average word length (too short = bad OCR)
    - Common OCR artifacts (repeated chars, garbled text)
    - "Noise lines" (lines that are mostly digits or special chars)
    """
    if not text or len(text.strip()) < 10:
        return 0.0

    lines = text.split("\n")
    if not lines:
        return 0.0

    # ── Check 1: Filter noise lines and compute printable ratio ──
    # A "noise line" is one where >80% of chars are digits or special chars
    signal_lines = []
    noise_line_count = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Count printable alphabetic chars vs total
        alpha_chars = sum(1 for c in stripped if c.isalpha() or c.isspace())
        total_chars = len(stripped)
        if total_chars == 0:
            continue
        alpha_ratio = alpha_chars / total_chars

        if alpha_ratio < 0.20:
            # Line is mostly digits/symbols — likely noise
            noise_line_count += 1
        else:
            signal_lines.append(stripped)

    signal_text = "\n".join(signal_lines)
    if not signal_text:
        return 0.1  # All noise lines — very low quality

    # ── Check 2: Printable character ratio in signal text ──
    total_chars = len(signal_text.replace(" ", ""))
    if total_chars == 0:
        return 0.1

    printable_chars = sum(1 for c in signal_text if c.isalnum() or c in ".,;:!?-'\"()/@#$%&+= ")
    printable_ratio = printable_chars / total_chars

    # Start score from printable ratio (0.0 - 1.0)
    quality = min(1.0, printable_ratio)

    # ── Check 3: Average word length ──
    words = [w for w in re.split(r"\s+", signal_text) if len(w) > 0]
    if not words:
        return 0.1

    avg_word_len = sum(len(w) for w in words) / len(words)
    # Very short average word length suggests bad OCR (garbage fragments)
    if avg_word_len < 2.0:
        quality *= 0.3
    elif avg_word_len < 3.0:
        quality *= 0.7
    # Good word length: no penalty

    # ── Check 4: Common OCR artifacts ──
    # Repeated characters (e.g., "1111111", ":::::")
    artifact_count = 0
    # Lines of repeated single characters (noise bars, borders)
    repeated_char_lines = sum(1 for line in signal_lines
                              if len(set(line.replace(" ", ""))) <= 2 and len(line.strip()) > 5)
    artifact_count += repeated_char_lines

    # Garbled text: high ratio of non-alpha, non-space characters
    garbled_fragments = sum(1 for word in words
                            if len(word) > 4 and sum(1 for c in word if not c.isalpha()) / len(word) > 0.5)
    artifact_count += garbled_fragments

    # Reduce quality for artifacts
    if artifact_count > 5:
        quality *= 0.7
    elif artifact_count > 2:
        quality *= 0.85

    # ── Check 5: Noise line ratio ──
    total_lines = len([l for l in lines if l.strip()])
    if total_lines > 0:
        noise_ratio = noise_line_count / total_lines
        if noise_ratio > 0.5:
            quality *= 0.5
        elif noise_ratio > 0.3:
            quality *= 0.75

    # ── Check 6: Meaningful content signals ──
    # Look for real words (common short words suggest real text)
    common_words = {
        "the", "and", "for", "are", "but", "not", "you", "all", "can",
        "had", "her", "was", "one", "our", "out", "has", "have", "this",
        "that", "with", "from", "they", "been", "will", "date", "name",
        "patient", "medical", "doctor", "prescription", "diagnosis",
        "bill", "invoice", "statement", "total", "amount", "due",
    }
    signal_lower = signal_text.lower()
    common_word_hits = sum(1 for w in common_words if w in signal_lower)
    if common_word_hits >= 5:
        quality = min(1.0, quality + 0.10)
    elif common_word_hits >= 2:
        quality = min(1.0, quality + 0.05)

    return max(0.0, min(1.0, quality))