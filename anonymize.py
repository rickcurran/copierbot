"""Headline anonymization helpers for safer satirical prompts."""

from __future__ import annotations

import hashlib
import re


# Match likely person-like name sequences (2-3 capitalized tokens).
NAME_PATTERN = re.compile(
    r"\b([A-Z][a-z]{2,}(?:[-'][A-Z][a-z]{2,})?)(\s+[A-Z][a-z]{2,}(?:[-'][A-Z][a-z]{2,})?){1,2}\b"
)

# Terms that indicate an organization rather than a person name.
ORG_HINTS = {
    "University",
    "Institute",
    "Company",
    "Inc",
    "Corp",
    "Corporation",
    "Ltd",
    "LLC",
    "Bank",
    "Agency",
    "Committee",
    "Ministry",
    "Department",
    "News",
    "Times",
    "Post",
    "Daily",
}


def _safe_mutate_token(token: str) -> str:
    """Generate a neutral pseudonym-style misspelling for one token."""
    if len(token) <= 3:
        return token

    digest = hashlib.sha1(token.encode("utf-8")).hexdigest()
    variant = int(digest[:2], 16) % 4
    lowered = token.lower()

    if variant == 0:
        # Light vowel shift.
        vowel_map = {"a": "e", "e": "i", "i": "o", "o": "u", "u": "a"}
        chars = list(lowered)
        for i, ch in enumerate(chars):
            if ch in vowel_map:
                chars[i] = vowel_map[ch]
                break
        mutated = "".join(chars)
    elif variant == 1:
        # Swap an internal adjacent pair.
        chars = list(lowered)
        index = 2 if len(chars) > 5 else 1
        chars[index], chars[index + 1] = chars[index + 1], chars[index]
        mutated = "".join(chars)
    elif variant == 2:
        # Soften consonant mapping.
        mutated = (
            lowered.replace("ph", "f")
            .replace("ck", "k")
            .replace("c", "k", 1)
            .replace("x", "ks", 1)
        )
    else:
        # Duplicate one internal consonant.
        chars = list(lowered)
        duplicated = False
        for i in range(1, len(chars) - 1):
            if chars[i] not in "aeiou":
                chars.insert(i, chars[i])
                duplicated = True
                break
        if not duplicated:
            chars.insert(1, chars[1])
        mutated = "".join(chars)

    return mutated[:1].upper() + mutated[1:]


def _looks_like_person_name(candidate: str) -> bool:
    """Heuristic filter for person-like names."""
    tokens = candidate.split()
    if len(tokens) < 2:
        return False
    if any(token in ORG_HINTS for token in tokens):
        return False
    return True


def anonymize_headline_names(headline: str) -> str:
    """Replace likely personal names with neutral pseudonym-style misspellings."""

    def _replace(match: re.Match[str]) -> str:
        full_name = match.group(0)
        if not _looks_like_person_name(full_name):
            return full_name
        parts = full_name.split()
        mutated_parts = [_safe_mutate_token(part) for part in parts]
        return " ".join(mutated_parts)

    return NAME_PATTERN.sub(_replace, headline)
