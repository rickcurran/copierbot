"""CLI entrypoint for Copierbot."""

import argparse
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from openai import OpenAI

from article_context import build_article_context
from ascii_fallback import create_ascii_fallback_image
from anonymize import anonymize_headline_names
from caption import generate_caption
from config import get_settings
from creative import generate_collage_concept_and_prompt
from image_gen import generate_image
from news import choose_headline, get_headlines
from persona import get_persona_context, get_persona_state, increment_post_counter
from phase_event import save_phase_change_system_log
from system_log import generate_system_log
from system_log_card import card_path_for_system_log, render_system_log_card
from title_gen import generate_title


OUTPUT_DIR = Path("output")


def get_generation_char_limit(settings) -> int:
    """Return cross-platform max chars for generated post text."""
    return min(settings.mastodon_max_chars, settings.bluesky_max_chars)


def setup_logging() -> None:
    """Configure readable console logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def save_text(path: Path, content: str) -> None:
    """Save text content to a file, creating parent directories if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")


def get_system_timestamp() -> str:
    """Return a timestamp string based on system clock down to seconds."""
    epoch_seconds = int(time.time())
    local_now = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).astimezone()
    return local_now.strftime("%Y-%m-%d-%H-%M-%S")


def build_output_paths() -> tuple[Path, Path, Path, Path, str]:
    """Create a unique timestamped output folder and return output file paths."""
    timestamp = get_system_timestamp()
    run_dir = OUTPUT_DIR / timestamp
    suffix = 1
    while run_dir.exists():
        run_dir = OUTPUT_DIR / f"{timestamp}-{suffix}"
        suffix += 1

    run_dir.mkdir(parents=True, exist_ok=False)
    image_path = run_dir / f"image  {timestamp}.jpg"
    prompt_path = run_dir / f"prompt  {timestamp}.txt"
    caption_path = run_dir / f"caption  {timestamp}.txt"
    system_log_path = run_dir / f"system_log  {timestamp}.txt"
    return image_path, prompt_path, caption_path, system_log_path, str(run_dir)


def choose_post_type() -> str:
    """Choose post type with 80/20 weighting (news/system_log)."""
    return "system_log" if random.random() < 0.2 else "news"


def run_news_post(
    client: OpenAI,
    settings,
    persona_context: str,
    image_path: Path,
    prompt_path: Path,
    caption_path: Path,
) -> None:
    """Generate a standard news collage post."""
    max_post_chars = get_generation_char_limit(settings)
    logging.info("Fetching current headlines...")
    headlines = get_headlines(
        news_api_key=settings.news_api_key,
        country=settings.news_country,
        page_size=settings.news_page_size,
    )

    selected_article = choose_headline(headlines)
    headline = selected_article["title"]
    article_url = selected_article["url"]
    logging.info("Selected headline: %s", headline)
    if article_url:
        logging.info("Selected article URL: %s", article_url)

    safe_headline = headline
    if settings.enable_name_obfuscation:
        safe_headline = anonymize_headline_names(headline)
        logging.info("Name obfuscation enabled.")
        logging.info("Headline used for generation: %s", safe_headline)
    else:
        logging.info("Name obfuscation disabled; using original headline.")

    article_context = build_article_context(
        headline=safe_headline,
        description=selected_article.get("description", ""),
        article_url=article_url,
    )
    story_context = article_context["story_context"]
    visual_cues = article_context["visual_cues"]
    source_meta_excerpt = article_context["source_meta_excerpt"]
    logging.info("Extracted %d visual cue(s) from source context.", len(visual_cues))

    logging.info("Generating short title...")
    title = generate_title(client=client, model=settings.text_model, headline=safe_headline)

    logging.info("Generating collage concept and prompt...")
    concept, image_prompt = generate_collage_concept_and_prompt(
        client=client,
        model=settings.text_model,
        headline=safe_headline,
        persona_context=persona_context,
        story_context=story_context,
        visual_cues=visual_cues,
    )
    logging.info("Concept: %s", concept)

    logging.info("Generating image...")
    image_render_mode = "openai_image"
    image_error = ""
    ascii_snapshot = ""
    try:
        final_prompt = generate_image(
            client=client,
            model=settings.image_model,
            prompt=image_prompt,
            output_path=image_path,
        )
        logging.info("Saved image to %s", image_path)
    except RuntimeError as exc:
        image_render_mode = "ascii_fallback"
        image_error = str(exc)
        logging.warning("Image generation failed. Creating ASCII fallback image...")
        ascii_snapshot = create_ascii_fallback_image(
            output_path=image_path,
            headline=safe_headline,
            persona_context=persona_context,
            error_message=image_error,
        )
        final_prompt = (
            f"{image_prompt}\n\n"
            "[Fallback engaged] OpenAI image generation failed. "
            "Rendered local ASCII-art image instead."
        )
        logging.info("Saved ASCII fallback image to %s", image_path)

    logging.info("Generating caption...")
    caption = generate_caption(
        client=client,
        model=settings.text_model,
        headline=safe_headline,
        persona_context=persona_context,
        max_chars=max_post_chars,
    )
    caption_output = f"{title}\n\n{caption}"
    save_text(caption_path, caption_output)
    logging.info("Saved caption to %s", caption_path)

    prompt_output = (
        f"Title: {title}\n"
        f"Original headline: {headline}\n"
        f"Article URL: {article_url or 'N/A'}\n"
        f"Headline used for generation: {safe_headline}\n\n"
        f"Story context used: {story_context or 'N/A'}\n"
        f"Source metadata excerpt: {source_meta_excerpt or 'N/A'}\n"
        f"Visual cues used: {', '.join(visual_cues) if visual_cues else 'N/A'}\n\n"
        f"Image render mode: {image_render_mode}\n"
        f"Image error context: {image_error or 'N/A'}\n\n"
        f"Final image prompt:\n{final_prompt}"
    )
    if ascii_snapshot:
        prompt_output += f"\n\nASCII fallback content:\n{ascii_snapshot}"
    save_text(prompt_path, prompt_output)
    logging.info("Saved final prompt to %s", prompt_path)


def run_system_log_post(client: OpenAI, settings, persona_context: str, system_log_path: Path) -> None:
    """Generate a system-log-only post."""
    logging.info("Generating system log post...")
    max_post_chars = get_generation_char_limit(settings)
    system_log = generate_system_log(
        client=client,
        model=settings.text_model,
        persona_context=persona_context,
        max_chars=max_post_chars,
    )
    save_text(system_log_path, system_log)
    logging.info("Saved system log to %s", system_log_path)
    card_path = card_path_for_system_log(system_log_path)
    try:
        render_system_log_card(system_log_text=system_log, output_path=card_path)
        logging.info("Saved system log card to %s", card_path)
    except Exception as exc:
        logging.warning("Failed to render system log card: %s", exc)


def _write_run_manifest(path: Path, run_dirs: list[Path]) -> None:
    """Write created run directories, one per line, preserving order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    unique: list[Path] = []
    seen: set[str] = set()
    for run_dir in run_dirs:
        normalized = str(run_dir.resolve())
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(run_dir.resolve())
    payload = "\n".join(str(item) for item in unique).strip()
    path.write_text((payload + "\n") if payload else "", encoding="utf-8")


def run(run_manifest_path: Path | None = None) -> None:
    """Execute the full Copierbot pipeline."""
    setup_logging()
    settings = get_settings()
    client = OpenAI(api_key=settings.openai_api_key)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    image_path, prompt_path, caption_path, system_log_path, run_dir = build_output_paths()
    logging.info("Output folder for this run: %s", run_dir)
    created_run_dirs: list[Path] = [Path(run_dir).resolve()]
    persona_context = get_persona_context()
    logging.info("Persona context loaded.")
    logging.info("Post mode: %s", settings.post_mode)
    logging.info(
        "Generation char limit: %d (min of Mastodon=%d, Bluesky=%d)",
        get_generation_char_limit(settings),
        settings.mastodon_max_chars,
        settings.bluesky_max_chars,
    )

    post_type = choose_post_type()
    logging.info("Selected post type: %s", post_type)
    if post_type == "system_log":
        run_system_log_post(
            client=client,
            settings=settings,
            persona_context=persona_context,
            system_log_path=system_log_path,
        )
    else:
        run_news_post(
            client=client,
            settings=settings,
            persona_context=persona_context,
            image_path=image_path,
            prompt_path=prompt_path,
            caption_path=caption_path,
        )

    previous_state = get_persona_state()
    new_state = increment_post_counter()
    logging.info(
        "Persona updated -> phase: %s, posts_generated: %d",
        new_state["phase"],
        new_state["posts_generated"],
    )
    if previous_state["phase"] != new_state["phase"]:
        phase_log_path = save_phase_change_system_log(
            output_root=OUTPUT_DIR,
            previous_phase=previous_state["phase"],
            new_phase=new_state["phase"],
            posts_generated=new_state["posts_generated"],
            max_chars=250,
        )
        logging.info("Saved phase-change system log to %s", phase_log_path)
        created_run_dirs.append(phase_log_path.parent.resolve())

    if run_manifest_path is not None:
        _write_run_manifest(run_manifest_path, created_run_dirs)
        logging.info("Wrote run manifest to %s", run_manifest_path)

    logging.info("Copierbot run complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Copierbot post(s).")
    parser.add_argument(
        "--run-manifest",
        default="",
        help="Optional path to write created output run directories (one per line).",
    )
    args = parser.parse_args()

    try:
        manifest_path = Path(args.run_manifest).resolve() if args.run_manifest else None
        run(run_manifest_path=manifest_path)
    except Exception as exc:
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s | %(message)s")
        logging.error("Pipeline failed: %s", exc)
        raise SystemExit(1)
