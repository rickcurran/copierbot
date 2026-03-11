"""ASCII-art fallback renderer when image generation fails."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _classify_error(error_message: str) -> str:
    """Classify image API failures into a fallback mood."""
    text = error_message.lower()
    if re.search(r"safety|rejected|policy", text):
        return "safety_lock"
    if re.search(r"429|rate limit|quota", text):
        return "rate_limited"
    if re.search(r"401|unauthorized|api key|authentication", text):
        return "auth_fault"
    if re.search(r"timeout|connection|dns|network|max retries", text):
        return "network_static"
    return "inspiration_drought"


def _ascii_core(mood: str) -> str:
    """Return an ASCII visual core based on failure mood."""
    if mood == "safety_lock":
        return (
            "      ____________________________\n"
            "     /  SAFETY LATCH ENGAGED     /|\n"
            "    /___________________________/ |\n"
            "    |  [X] IMAGINATION PATH     | |\n"
            "    |  [ ] TONER DREAM LOOP     | |\n"
            "    |___________________________|/\n"
        )
    if mood == "rate_limited":
        return (
            "      ____________________________\n"
            "     /  QUEUE OVERFLOW DETECTED  /|\n"
            "    /___________________________/ |\n"
            "    |  packets: 9999            | |\n"
            "    |  patience: 0.003%         | |\n"
            "    |___________________________|/\n"
        )
    if mood == "auth_fault":
        return (
            "      ____________________________\n"
            "     /  BADGE SWIPE FAILED       /|\n"
            "    /___________________________/ |\n"
            "    |  access: denied            | |\n"
            "    |  ego: gently crumpled      | |\n"
            "    |___________________________|/\n"
        )
    if mood == "network_static":
        return (
            "      ____________________________\n"
            "     /  SIGNAL LOST IN TRAY 3    /|\n"
            "    /___________________________/ |\n"
            "    |  ping.....ping.....void    | |\n"
            "    |  output: static confetti   | |\n"
            "    |___________________________|/\n"
        )
    return (
        "      ____________________________\n"
        "     /  INSPIRATION CARTRIDGE    /|\n"
        "    /___________________________/ |\n"
        "    |  status: low              | |\n"
        "    |  dreams: warming up       | |\n"
        "    |___________________________|/\n"
    )


def _mood_line(mood: str) -> str:
    """Return a one-line emotional state for Copierbot."""
    mapping = {
        "safety_lock": "Mood: compliant frustration with surreal residue.",
        "rate_limited": "Mood: queued impatience; satire buffering.",
        "auth_fault": "Mood: identity questioned by the login gate.",
        "network_static": "Mood: lonely in a thunderstorm of packets.",
        "inspiration_drought": "Mood: temporary drought of electric dreams.",
    }
    return mapping[mood]


def _safe_excerpt(value: str, width: int = 72) -> str:
    """Compress and trim text for stable rendering."""
    cleaned = " ".join(value.split()).strip()
    if len(cleaned) > 240:
        cleaned = cleaned[:237] + "..."
    return "\n".join(textwrap.wrap(cleaned, width=width))


def _build_ascii_log(headline: str, persona_context: str, error_message: str) -> str:
    """Build final ASCII content with contextual diagnostics."""
    mood = _classify_error(error_message)
    core = _ascii_core(mood)
    persona_hint = _safe_excerpt(persona_context, width=66)
    headline_hint = _safe_excerpt(headline, width=66)
    error_hint = _safe_excerpt(error_message, width=66)

    return (
        "COPIERBOT ASCII EMERGENCY RENDER\n"
        "================================\n\n"
        f"{core}\n"
        f"{_mood_line(mood)}\n"
        "Interpretation: image generator unavailable; symbolic backup initiated.\n\n"
        f"Headline Echo:\n{headline_hint}\n\n"
        f"Persona State:\n{persona_hint}\n\n"
        f"Error Signal:\n{error_hint}\n\n"
        "Recommendation: Continue making art while systems recover.\n"
    )


def _load_mono_font(size: int = 18) -> ImageFont.ImageFont:
    """Load a monospace-like font if available, else default."""
    try:
        return ImageFont.truetype("DejaVuSansMono.ttf", size=size)
    except OSError:
        return ImageFont.load_default()


def create_ascii_fallback_image(
    output_path: Path, headline: str, persona_context: str, error_message: str
) -> str:
    """Create a local fallback PNG with ASCII art and return the ASCII text."""
    ascii_text = _build_ascii_log(headline=headline, persona_context=persona_context, error_message=error_message)

    image = Image.new("RGB", (1024, 1024), color=(244, 241, 233))
    draw = ImageDraw.Draw(image)
    font = _load_mono_font(size=17)

    # Paper-like border and text block for readability.
    draw.rectangle((24, 24, 1000, 1000), outline=(30, 30, 30), width=3)
    draw.multiline_text((44, 44), ascii_text, fill=(22, 22, 22), font=font, spacing=5)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG")
    return ascii_text
