"""Configuration helpers for Copierbot."""

from dataclasses import dataclass
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """Application settings loaded from environment variables."""

    openai_api_key: str
    news_api_key: str
    news_country: str = "us"
    news_page_size: int = 40
    text_model: str = "gpt-4.1-mini"
    image_model: str = "gpt-image-1"
    post_mode: str = "default"
    mastodon_max_chars: int = 500


def get_settings(
    require_news_api_key: bool = True, require_openai_api_key: bool = True
) -> Settings:
    """Load environment variables and return validated settings."""
    load_dotenv()

    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    news_api_key = os.getenv("NEWS_API_KEY", "").strip()

    missing = []
    if require_openai_api_key and not openai_api_key:
        missing.append("OPENAI_API_KEY")
    if require_news_api_key and not news_api_key:
        missing.append("NEWS_API_KEY")

    if missing:
        joined = ", ".join(missing)
        raise ValueError(
            f"Missing required environment variables: {joined}. "
            "Copy .env.example to .env and fill the keys."
        )

    news_country = os.getenv("NEWS_COUNTRY", "us").strip().lower() or "us"
    news_page_size_raw = os.getenv("NEWS_PAGE_SIZE", "40").strip()
    try:
        news_page_size = int(news_page_size_raw)
    except ValueError:
        news_page_size = 40

    post_mode = os.getenv("POST_MODE", "default").strip().lower() or "default"
    if post_mode not in {"default", "mastodon"}:
        post_mode = "default"

    max_chars_raw = os.getenv("MASTODON_MAX_CHARS", "500").strip()
    try:
        mastodon_max_chars = int(max_chars_raw)
    except ValueError:
        mastodon_max_chars = 500
    mastodon_max_chars = max(100, min(mastodon_max_chars, 5000))

    return Settings(
        openai_api_key=openai_api_key,
        news_api_key=news_api_key,
        news_country=news_country,
        news_page_size=max(10, min(news_page_size, 100)),
        post_mode=post_mode,
        mastodon_max_chars=mastodon_max_chars,
    )
