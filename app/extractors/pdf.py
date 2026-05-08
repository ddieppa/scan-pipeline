from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

from app.extractors.common import ExtractionResult, ocr_image_file

logger = logging.getLogger(__name__)

# ── Fast path: pdftotext (poppler-utils) ──────────────────────────
# Native text extraction via poppler is ~10-50x faster than PyMuPDF+OCR.
# Try this first; only fall back to PyMuPDF+OCR if it fails or yields
# too little text.

def _try_pdftotext(path: Path, timeout_seconds: int = 15) -> tuple[str, int]:
    """Extract text using pdftotext. Returns (text, pages)."""
    text = ""
    pages = 0
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
        logger.debug("pdftotext not found, skipping fast path")
    except subprocess.TimeoutExpired:
        logger.warning(f"pdftotext timed out on {path.name}")
    # Get page count via pdfinfo
    try:
        info = subprocess.run(
            ["pdfinfo", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if info.returncode == 0:
            for line in info.stdout.split("\n"):
                if line.lower().startswith("pages:"):
                    try:
                        pages = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                    break
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return text, pages


# ── Slow path: PyMuPDF + per-page OCR fallback ────────────────────
# Used only when pdftotext yields insufficient text (scanned PDFs,
# image-only PDFs, etc.)

def _extract_with_pymupdf(
    path: Path,
    inspect_pages: int,
    min_text_chars_before_skip_ocr: int,
    render_dpi: int,
    ocr_timeout_per_page: int = 20,
) -> ExtractionResult:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required for PDF extraction") from exc

    # ── Phase 1: Pass through all pages to collect native text and detect
    #    whether this is an image-only (scanned) PDF.
    native_chunks: list[str] = []
    pages_with_images = 0
    pages = 0
    total_pages = 0

    with fitz.open(path) as doc:
        total_pages = len(doc)
        pages_to_inspect = min(total_pages, inspect_pages)
        for page_index in range(pages_to_inspect):
            page = doc.load_page(page_index)
            pages += 1
            page_text = page.get_text("text").strip()
            if page_text:
                native_chunks.append(page_text)
            if page.get_images(full=True):
                pages_with_images += 1

        # ── Determine if OCR is needed ──
        # Image-only PDF: all inspected pages have images and no native text.
        # These need full OCR on every page (the "min_text" skip would
        # short-circuit after the first page of OCR gibberish like mailing
        # instructions, missing the actual content on later pages).
        is_image_only = pages_with_images >= pages and not native_chunks
        has_enough_native = len(" ".join(native_chunks)) >= min_text_chars_before_skip_ocr

        if has_enough_native:
            # Native text is sufficient — no OCR needed at all
            combined = "\n".join(chunk for chunk in native_chunks if chunk).strip()
            return ExtractionResult(
                text=combined,
                text_source="native_pdf",
                pages_inspected=pages,
                needs_ocr=False,
                metadata={"pages_with_images": pages_with_images},
            )

        # ── Phase 2: OCR pages that need it ──
        text_chunks: list[str] = list(native_chunks)  # start with any native text
        ocr_used = False

        for page_index in range(pages_to_inspect):
            page = doc.load_page(page_index)
            page_text = page.get_text("text").strip()

            # For image-only PDFs: OCR every page (don't short-circuit)
            # For mixed PDFs: only OCR pages with no text or image-only content
            needs_ocr = is_image_only or (not page_text) or page.get_images(full=True)
            if not needs_ocr:
                continue
            # For image-only PDFs, skip the skip-check; for mixed, stop once we have enough
            if not is_image_only and len(" ".join(text_chunks)) >= min_text_chars_before_skip_ocr:
                continue

            try:
                with tempfile.TemporaryDirectory(prefix="scan-pdf-") as tmpdir:
                    image_path = Path(tmpdir) / f"page-{page_index + 1}.png"
                    page.get_pixmap(dpi=render_dpi).save(image_path)
                    ocr_text = ocr_image_file(image_path, timeout_seconds=ocr_timeout_per_page)
                    if ocr_text:
                        text_chunks.append(ocr_text)
                        ocr_used = True
            except Exception as exc:
                logger.warning(f"OCR failed for page {page_index + 1} of {path.name}: {exc}")

    combined = "\n".join(chunk for chunk in text_chunks if chunk).strip()
    return ExtractionResult(
        text=combined,
        text_source="ocr_pdf" if ocr_used else "native_pdf",
        pages_inspected=pages,
        needs_ocr=ocr_used or (pages > 0 and len(combined) < min_text_chars_before_skip_ocr and pages_with_images > 0),
        metadata={"pages_with_images": pages_with_images},
    )


# ── Main entry point ──────────────────────────────────────────────

def extract_pdf(
    path: Path,
    inspect_pages: int,
    min_text_chars_before_skip_ocr: int,
    render_dpi: int,
    ocr_timeout_per_page: int = 20,
) -> ExtractionResult:
    """Extract text from a PDF file.

    Strategy:
    1. Try pdftotext (poppler-utils) — fast, native text extraction
    2. If that yields enough text, return immediately (no OCR needed)
    3. If text is insufficient (< min_text_chars_before_skip_ocr),
       fall back to PyMuPDF + per-page OCR for scanned/image PDFs

    This avoids the slow PyMuPDF+OCR path for normal text PDFs,
    which was causing 60-120s per file in the old code.
    """
    # ── Fast path: pdftotext ──
    native_text, page_count = _try_pdftotext(path)

    if len(native_text.strip()) >= min_text_chars_before_skip_ocr:
        return ExtractionResult(
            text=native_text.strip(),
            text_source="pdftotext",
            pages_inspected=page_count,
            needs_ocr=False,
            metadata={"pages_with_images": 0},
        )

    # ── Slow path: PyMuPDF + OCR ──
    logger.info(f"pdftotext yielded only {len(native_text.strip())} chars for {path.name}, falling back to PyMuPDF+OCR")
    return _extract_with_pymupdf(
        path,
        inspect_pages,
        min_text_chars_before_skip_ocr,
        render_dpi,
        ocr_timeout_per_page=ocr_timeout_per_page,
    )