"""Article context extraction helpers for story-grounded image prompts."""

from __future__ import annotations

from html.parser import HTMLParser
import re
from typing import TypedDict
from urllib.parse import urlparse

import requests


class ArticleContext(TypedDict):
    """Context extracted from source article metadata/content."""

    story_context: str
    visual_cues: list[str]
    source_meta_excerpt: str


INTERESTING_META_KEYS = {
    "og:title",
    "og:description",
    "twitter:title",
    "twitter:description",
    "twitter:image:alt",
    "og:image:alt",
}


class _ArticleMetaParser(HTMLParser):
    """Parse key metadata and image alt hints from article HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        self.image_alts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        lowered = tag.lower()
        attr_map = {str(k).lower(): str(v) for k, v in attrs}

        if lowered == "meta":
            key = (attr_map.get("property") or attr_map.get("name") or "").lower().strip()
            value = (attr_map.get("content") or "").strip()
            if key in INTERESTING_META_KEYS and value:
                self.meta[key] = value

        if lowered == "img":
            alt = (attr_map.get("alt") or "").strip()
            if 12 <= len(alt) <= 220 and alt not in self.image_alts:
                self.image_alts.append(alt)


def _clean_text(text: str) -> str:
    """Normalize whitespace for stable prompt input."""
    return " ".join((text or "").split()).strip()


def _looks_like_asset_name(text: str) -> bool:
    """Detect low-value filename-like strings."""
    lowered = text.lower().strip()
    if re.search(r"\.(jpg|jpeg|png|gif|webp)$", lowered):
        return True
    if "/" in lowered or "\\" in lowered:
        return True
    separators = lowered.count("-") + lowered.count("_")
    digits = sum(ch.isdigit() for ch in lowered)
    if separators >= 3 and digits >= 2 and " " not in lowered:
        return True
    return False


def _slug_tokens(url: str) -> list[str]:
    """Extract readable tokens from URL path as weak story hints."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return []
    path = parsed.path or ""
    chunks = re.split(r"[^a-zA-Z0-9]+", path)
    tokens = [chunk.lower() for chunk in chunks if len(chunk) >= 4 and not chunk.isdigit()]
    deduped: list[str] = []
    seen = set()
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return deduped[:8]


def _fetch_article_html(article_url: str, timeout: int = 20) -> str:
    """Fetch article HTML safely with a browser-like user agent."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        )
    }
    response = requests.get(article_url, timeout=timeout, headers=headers)
    response.raise_for_status()
    return response.text


def build_article_context(headline: str, description: str, article_url: str) -> ArticleContext:
    """Build story + visual context from headline, description, and article metadata."""
    cleaned_headline = _clean_text(headline)
    cleaned_description = _clean_text(description)

    story_parts = [cleaned_headline]
    if cleaned_description:
        story_parts.append(cleaned_description)

    visual_cues: list[str] = []
    source_meta_excerpt_parts: list[str] = []

    if article_url:
        try:
            html = _fetch_article_html(article_url)
            parser = _ArticleMetaParser()
            parser.feed(html)

            for key in ("og:title", "twitter:title", "og:description", "twitter:description"):
                value = _clean_text(parser.meta.get(key, ""))
                if value and value not in source_meta_excerpt_parts:
                    source_meta_excerpt_parts.append(value)

            for key in ("og:image:alt", "twitter:image:alt"):
                value = _clean_text(parser.meta.get(key, ""))
                if value and not _looks_like_asset_name(value) and value not in visual_cues:
                    visual_cues.append(value)

            for alt in parser.image_alts[:8]:
                cleaned_alt = _clean_text(alt)
                if cleaned_alt and not _looks_like_asset_name(cleaned_alt) and cleaned_alt not in visual_cues:
                    visual_cues.append(cleaned_alt)
                if len(visual_cues) >= 8:
                    break

            if not visual_cues:
                for excerpt in source_meta_excerpt_parts[:3]:
                    if excerpt and excerpt not in visual_cues:
                        visual_cues.append(excerpt)
        except requests.RequestException:
            # Non-fatal: context extraction should not break the generation pipeline.
            pass

    if not visual_cues:
        fallback_tokens = _slug_tokens(article_url)
        if fallback_tokens:
            visual_cues.append("URL motifs: " + ", ".join(fallback_tokens))

    story_context = " | ".join(part for part in story_parts if part)
    source_meta_excerpt = " | ".join(source_meta_excerpt_parts[:3])

    return {
        "story_context": story_context,
        "visual_cues": visual_cues[:8],
        "source_meta_excerpt": source_meta_excerpt,
    }
