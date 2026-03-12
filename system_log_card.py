"""Render system-log text onto a branded template image using Pillow."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TEMPLATE_PATH = BASE_DIR / "assets/templates/system_log_card.png"
FONT_CANDIDATES = [
    Path("/System/Library/Fonts/Supplemental/Courier New.ttf"),
    Path("/System/Library/Fonts/Menlo.ttc"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    Path("C:/Windows/Fonts/consola.ttf"),
]


def card_path_for_system_log(system_log_path: Path) -> Path:
    """Return a card image path matching system_log timestamp naming."""
    stem = system_log_path.stem
    if "  " in stem:
        timestamp = stem.split("  ", 1)[1]
    else:
        timestamp = stem
    return system_log_path.parent / f"system_log_card  {timestamp}.png"


def _pick_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load preferred monospaced font, falling back to default."""
    for candidate in FONT_CANDIDATES:
        if candidate.exists():
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _measure_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    """Return width and height for one line of text."""
    bbox = draw.textbbox((0, 0), text, font=font)
    return max(0, bbox[2] - bbox[0]), max(0, bbox[3] - bbox[1])


def _wrap_text_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    """Wrap text into lines constrained by max pixel width."""
    paragraphs = text.splitlines() or [""]
    wrapped: list[str] = []

    for paragraph in paragraphs:
        words = paragraph.split()
        if not words:
            wrapped.append("")
            continue

        line = words[0]
        for word in words[1:]:
            candidate = f"{line} {word}"
            width, _ = _measure_text(draw, candidate, font)
            if width <= max_width:
                line = candidate
            else:
                wrapped.append(line)
                line = word
        wrapped.append(line)

    return wrapped


def _choose_text_color(image: Image.Image, text_box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Choose black/white text based on template brightness in text region."""
    x0, y0, x1, y1 = text_box
    region = image.convert("L").crop((x0, y0, x1, y1))
    histogram = region.histogram()
    pixel_count = max(1, region.width * region.height)
    brightness = sum(i * count for i, count in enumerate(histogram)) / pixel_count
    return (245, 245, 245, 255) if brightness < 128 else (20, 20, 20, 255)


def _iter_font_sizes(start: int = 56, end: int = 16) -> Iterable[int]:
    """Iterate descending font sizes to fit content."""
    return range(start, end - 1, -2)


def _multiline_block_height(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.ImageFont,
    spacing: int,
) -> int:
    """Measure exact multiline text block height using Pillow's bbox."""
    content = "\n".join(lines) if lines else ""
    bbox = draw.multiline_textbbox((0, 0), content, font=font, spacing=spacing, align="left")
    return max(0, bbox[3] - bbox[1])


def render_system_log_card(
    system_log_text: str,
    output_path: Path,
    template_path: Path = DEFAULT_TEMPLATE_PATH,
) -> Path:
    """Render system-log text into a PNG card based on template."""
    if not template_path.exists():
        raise FileNotFoundError(f"System-log card template not found: {template_path}")

    image = Image.open(template_path).convert("RGBA")
    draw = ImageDraw.Draw(image)
    width, height = image.size

    text_box_candidates = [
        (
            int(width * 0.08),
            int(height * 0.14),
            int(width * 0.92),
            int(height * 0.90),
        ),
        (
            int(width * 0.06),
            int(height * 0.11),
            int(width * 0.94),
            int(height * 0.92),
        ),
        (
            int(width * 0.04),
            int(height * 0.08),
            int(width * 0.96),
            int(height * 0.94),
        ),
    ]

    text_box = text_box_candidates[0]
    final_lines: list[str] | None = None
    final_font: ImageFont.ImageFont | None = None
    final_spacing = 8

    for candidate_box in text_box_candidates:
        x0, y0, x1, y1 = candidate_box
        max_width = x1 - x0
        max_height = y1 - y0
        for size in _iter_font_sizes(start=56, end=8):
            font = _pick_font(size)
            lines = _wrap_text_lines(draw, system_log_text.strip(), font, max_width=max_width)
            _, line_height = _measure_text(draw, "Ag", font)
            spacing = max(1, int(line_height * 0.28))
            total_height = _multiline_block_height(draw, lines, font, spacing)

            if total_height <= max_height:
                text_box = candidate_box
                final_lines = lines
                final_font = font
                final_spacing = spacing
                break
        if final_lines is not None and final_font is not None:
            break

    if final_lines is None or final_font is None:
        x0, y0, x1, y1 = text_box_candidates[-1]
        max_width = x1 - x0
        max_height = y1 - y0
        final_font = _pick_font(8)
        final_lines = _wrap_text_lines(draw, system_log_text.strip(), final_font, max_width=max_width)
        _, line_height = _measure_text(draw, "Ag", final_font)
        final_spacing = max(1, int(line_height * 0.15))
        total_height = _multiline_block_height(draw, final_lines, final_font, final_spacing)
        if total_height > max_height:
            raise RuntimeError(
                "System log text is too long to fit on card template. "
                "Reduce text length or adjust template text region."
            )

    x0, y0, x1, y1 = text_box
    text_color = _choose_text_color(image, text_box)

    draw.multiline_text(
        (x0, y0),
        "\n".join(final_lines),
        font=final_font,
        fill=text_color,
        spacing=final_spacing,
        align="left",
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG")
    return output_path
