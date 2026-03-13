"""Image generation utilities."""

import base64
from io import BytesIO
from pathlib import Path
import re

import requests
from openai import OpenAI
from PIL import Image


STYLE_INSTRUCTIONS = (
    "surreal torn-paper photoshop collage, mixed-media photomontage, rough ripped edges, "
    "visible glue seams, cut-out magazine fragments, halftone print texture, photocopy grain, "
    "avant-garde surrealist composition, visionary dreamscape atmosphere, uncanny symbolic juxtapositions, "
    "impossible perspective and scale, satirical editorial social commentary, robots and office machines, "
    "occasional gaming and sci-fi references, non-literal storytelling"
)

SAFETY_GUARDRAILS = (
    "Use only fictional characters and original robot archetypes. "
    "Do not depict real people, celebrities, politicians, public figures, or recognizable individuals. "
    "Do not reference trademarked characters, franchise names, brand names, or logos. "
    "No readable text, letters, words, captions, titles, or watermarks inside the image. "
    "Keep content non-violent and editorially satirical."
)


def _build_final_prompt(prompt: str, strict_safety: bool) -> str:
    """Build the final image prompt with style and safety constraints."""
    strict_suffix = ""
    if strict_safety:
        strict_suffix = (
            " Simplify scene to abstract/fantastical robots and office objects only. "
            "Avoid portraits or likenesses of any real person. "
            "Keep the composition surreal and symbolic rather than literal."
        )
    return f"{prompt}. Style: {STYLE_INSTRUCTIONS}. Safety constraints: {SAFETY_GUARDRAILS}.{strict_suffix}"


def _save_image_for_path(image: Image.Image, output_path: Path) -> None:
    """Save image based on output extension (JPG default, PNG fallback)."""
    suffix = output_path.suffix.lower()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if suffix in {".jpg", ".jpeg"}:
        if image.mode == "RGBA":
            bg = Image.new("RGBA", image.size, (255, 255, 255, 255))
            bg.alpha_composite(image)
            image = bg.convert("RGB")
        else:
            image = image.convert("RGB")
        image.save(output_path, format="JPEG", quality=92, optimize=True, progressive=True)
        return

    image.save(output_path, format="PNG")


def generate_image(client: OpenAI, model: str, prompt: str, output_path: Path) -> str:
    """Generate a 1024x1024 image with OpenAI and save it to disk."""
    final_prompt = _build_final_prompt(prompt, strict_safety=False)

    try:
        result = client.images.generate(model=model, prompt=final_prompt, size="1024x1024")
    except Exception as exc:
        message = str(exc)
        if re.search(r"safety|rejected", message, flags=re.IGNORECASE):
            safer_prompt = _build_final_prompt(prompt, strict_safety=True)
            try:
                result = client.images.generate(model=model, prompt=safer_prompt, size="1024x1024")
                final_prompt = safer_prompt
            except Exception as retry_exc:
                raise RuntimeError(
                    "Failed to generate image after safety retry. "
                    f"Original error: {message}. Retry error: {retry_exc}"
                ) from retry_exc
        else:
            raise RuntimeError(f"Failed to generate image: {exc}") from exc

    image_data = getattr(result.data[0], "b64_json", None)
    if image_data:
        raw_bytes = base64.b64decode(image_data)
    else:
        url = getattr(result.data[0], "url", "")
        if not url:
            raise RuntimeError("OpenAI image response did not include image data.")
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            raw_bytes = response.content
        except requests.RequestException as exc:
            raise RuntimeError(f"Failed to download generated image: {exc}") from exc

    try:
        image = Image.open(BytesIO(raw_bytes))
        _save_image_for_path(image=image, output_path=output_path)
    except Exception as exc:
        raise RuntimeError(f"Failed to save image to disk: {exc}") from exc

    return final_prompt
