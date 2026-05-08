from __future__ import annotations

from pathlib import Path

from app.extractors.common import ExtractionResult, ocr_image_file


def extract_image(path: Path, max_ocr_chars: int) -> ExtractionResult:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required for image extraction") from exc

    with Image.open(path) as image:
        image.verify()

    with Image.open(path) as image:
        metadata = {"format": image.format, "size": image.size, "mode": image.mode}

    text = ocr_image_file(path)[:max_ocr_chars]
    return ExtractionResult(
        text=text,
        text_source="ocr_image" if text else "none",
        pages_inspected=1,
        needs_ocr=True,
        metadata=metadata,
    )
