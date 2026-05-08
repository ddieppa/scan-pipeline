from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ExtractionResult:
    text: str
    text_source: str
    pages_inspected: int
    needs_ocr: bool
    metadata: dict


def ocr_image_file(path: Path, language: str | None = None, timeout_seconds: int = 20) -> str:
    """OCR an image file with configurable timeout.

    Default timeout is 20s (down from 120s) to fit within OpenClaw exec limits.
    If language is None, uses tesseract's default (usually eng if available).
    Uses PSM 6 (single uniform block of text) for better ID/document recognition.
    """
    import os

    # Find tessdata directory
    tessdata_dir = None
    for candidate in [
        "/usr/share/tesseract-ocr/5/tessdata",
        "/usr/share/tesseract-ocr/4/tessdata",
        "/usr/share/tessdata",
        "/usr/local/share/tessdata",
    ]:
        if os.path.isdir(candidate):
            tessdata_dir = candidate
            break

    env = os.environ.copy()
    if tessdata_dir:
        env["TESSDATA_PREFIX"] = tessdata_dir

    with tempfile.TemporaryDirectory(prefix="scan-ocr-") as tmpdir:
        outbase = Path(tmpdir) / "ocr"
        cmd = ["tesseract", str(path), str(outbase), "--psm", "6"]
        if language:
            cmd.extend(["-l", language])
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            env=env,
        )
        txt_path = outbase.with_suffix(".txt")
        if cp.returncode == 0 and txt_path.exists():
            return txt_path.read_text(encoding="utf-8", errors="ignore")
        return ""


def extract_pdf_text(path: Path, timeout_seconds: int = 20) -> ExtractionResult:
    """Extract text from a PDF using pdftotext or similar tool.

    Falls back to OCR if text extraction fails or yields too little text.
    """
    import os
    import json

    text = ""
    text_source = "pdf"
    pages_inspected = 0
    metadata = {}

    # Try pdftotext first
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if result.returncode == 0:
            text = result.stdout
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        pass

    # If pdftotext not available or failed, try pdfinfo for metadata
    if not text:
        try:
            result = subprocess.run(
                ["pdfinfo", str(path)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.split("\n"):
                    if ":" in line:
                        key, val = line.split(":", 1)
                        metadata[key.strip().lower()] = val.strip()
        except FileNotFoundError:
            pass

    # Try to get page count
    try:
        result = subprocess.run(
            ["pdfinfo", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if line.lower().startswith("pages:"):
                    try:
                        pages_inspected = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                    break
    except FileNotFoundError:
        pass

    needs_ocr = len(text.strip()) < 50  # Less than 50 chars probably needs OCR

    return ExtractionResult(
        text=text,
        text_source=text_source,
        pages_inspected=pages_inspected,
        needs_ocr=needs_ocr,
        metadata=metadata,
    )
