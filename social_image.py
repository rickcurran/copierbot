"""Prepare social-upload image composites using a branded template."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TEMPLATE_PATH = BASE_DIR / "assets/templates/system_log_card.png"

# Template dimensions: 1080x1080, with a 1000x1000 image well at (40, 50).
COMPOSITE_WIDTH = 1080
COMPOSITE_HEIGHT = 1080
IMAGE_WELL_X = 40
IMAGE_WELL_Y = 50
IMAGE_WELL_SIZE = 1000


def social_composite_path_for_image(source_image_path: Path) -> Path:
    """Return a stable social composite path in the same run folder."""
    stem = source_image_path.stem
    timestamp = stem.split("  ", 1)[1] if "  " in stem else stem
    return source_image_path.parent / f"social_image  {timestamp}.jpg"


def build_social_composite_image(
    source_image_path: Path,
    template_path: Path = DEFAULT_TEMPLATE_PATH,
) -> Path:
    """Create social upload composite from template + generated image."""
    if not source_image_path.exists():
        raise FileNotFoundError(f"Source image not found: {source_image_path}")
    if not template_path.exists():
        raise FileNotFoundError(f"Template image not found: {template_path}")

    output_path = social_composite_path_for_image(source_image_path)
    if output_path.exists():
        output_mtime = output_path.stat().st_mtime
        source_mtime = source_image_path.stat().st_mtime
        template_mtime = template_path.stat().st_mtime
        if output_mtime >= max(source_mtime, template_mtime):
            return output_path

    template = Image.open(template_path).convert("RGB")
    if template.size != (COMPOSITE_WIDTH, COMPOSITE_HEIGHT):
        raise RuntimeError(
            "Unexpected social template size. "
            f"Expected {(COMPOSITE_WIDTH, COMPOSITE_HEIGHT)}, got {template.size}."
        )

    source = Image.open(source_image_path).convert("RGB")
    fitted = ImageOps.fit(
        source,
        (IMAGE_WELL_SIZE, IMAGE_WELL_SIZE),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )

    composite = template.copy()
    composite.paste(fitted, (IMAGE_WELL_X, IMAGE_WELL_Y))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    composite.save(output_path, format="JPEG", quality=92, optimize=True, progressive=True)
    return output_path
