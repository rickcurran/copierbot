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

# Common non-person tokens that frequently appear in tech/product headlines.
NON_PERSON_TOKENS = {
    "Apple",
    "Google",
    "Play",
    "Store",
    "Android",
    "iPhone",
    "iPad",
    "Mac",
    "MacBook",
    "Microsoft",
    "Windows",
    "Xbox",
    "Sony",
    "PlayStation",
    "Nintendo",
    "Steam",
    "Valve",
    "Meta",
    "Instagram",
    "Facebook",
    "WhatsApp",
    "Threads",
    "OpenAI",
    "ChatGPT",
    "Anthropic",
    "Claude",
    "Amazon",
    "AWS",
    "Prime",
    "Tesla",
    "Nvidia",
    "Intel",
    "AMD",
    "Samsung",
    "TikTok",
    "YouTube",
    "Netflix",
    "Disney",
    "Spotify",
    "GitHub",
    "Reddit",
    "Zoom",
    "Slack",
    "Cloudflare",
    "Oracle",
    "Salesforce",
}

# Conservative first-name hints used to reduce false positives.
FIRST_NAME_HINTS = {
    "Aaron",
    "Adam",
    "Aisha",
    "Alan",
    "Alex",
    "Alice",
    "Amina",
    "Andrew",
    "Angela",
    "Anna",
    "Anthony",
    "Ava",
    "Ben",
    "Benjamin",
    "Brian",
    "Carla",
    "Carlos",
    "Catherine",
    "Charlie",
    "Chris",
    "Daniel",
    "David",
    "Diana",
    "Emily",
    "Emma",
    "Eric",
    "Ethan",
    "Fatima",
    "Francesca",
    "Gabriel",
    "Grace",
    "Hannah",
    "Harper",
    "Henry",
    "Isabella",
    "Jack",
    "Jacob",
    "James",
    "Jason",
    "Jennifer",
    "Jessica",
    "John",
    "Jonathan",
    "Jordan",
    "Joseph",
    "Joshua",
    "Julia",
    "Karen",
    "Kevin",
    "Liam",
    "Linda",
    "Lisa",
    "Lucas",
    "Maya",
    "Michael",
    "Michelle",
    "Mohamed",
    "Muhammad",
    "Natalie",
    "Noah",
    "Olivia",
    "Omar",
    "Patricia",
    "Paul",
    "Peter",
    "Priya",
    "Rahul",
    "Richard",
    "Robert",
    "Ryan",
    "Samantha",
    "Sara",
    "Sarah",
    "Scott",
    "Sofia",
    "Sophia",
    "Stefan",
    "Stephen",
    "Steven",
    "Sundar",
    "Thomas",
    "Tim",
    "Victoria",
    "William",
    "Yusuf",
    "Zohran",
    "Zoe",
}

PERSON_TITLE_TOKENS = {
    "mr",
    "mrs",
    "ms",
    "miss",
    "mx",
    "dr",
    "prof",
    "professor",
    "president",
    "mayor",
    "senator",
    "governor",
    "ceo",
    "founder",
    "actor",
    "actress",
    "singer",
    "director",
    "coach",
}

PERSON_TITLE_HINT_RE = re.compile(
    r"(?:^|\W)(mr|mrs|ms|miss|mx|dr|prof|professor|president|prime minister|pm|mayor|senator|governor|ceo|founder|actor|actress|singer|director|coach)\.?\s*$",
    re.IGNORECASE,
)


def _clean_token(token: str) -> str:
    """Normalize token for set membership checks."""
    return re.sub(r"[^A-Za-z]", "", token)


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
    """Conservative heuristic filter for person-like names."""
    tokens = candidate.split()
    if len(tokens) < 2:
        return False

    cleaned = [_clean_token(token) for token in tokens]
    if any(not token for token in cleaned):
        return False
    if any(token in ORG_HINTS for token in cleaned):
        return False
    if any(token in NON_PERSON_TOKENS for token in cleaned):
        return False
    if any(token.isupper() and len(token) > 1 for token in cleaned):
        return False

    has_title_token = cleaned[0].lower() in PERSON_TITLE_TOKENS
    if has_title_token:
        return True

    # Keep this conservative for 2-token phrases to avoid brand/product false positives.
    if len(cleaned) == 2:
        return cleaned[0] in FIRST_NAME_HINTS

    # 3-token sequences often include middle names/initial-like structures.
    return cleaned[0] in FIRST_NAME_HINTS or cleaned[1] in FIRST_NAME_HINTS


def anonymize_headline_names(headline: str) -> str:
    """Replace likely personal names with neutral pseudonym-style misspellings."""

    def _replace(match: re.Match[str]) -> str:
        full_name = match.group(0)
        # If directly preceded by a human-title hint, treat as a person even if uncommon.
        prefix = headline[max(0, match.start() - 40) : match.start()]
        title_hint = bool(PERSON_TITLE_HINT_RE.search(prefix))
        if not title_hint and not _looks_like_person_name(full_name):
            return full_name
        parts = full_name.split()
        cleaned_parts = [_clean_token(part) for part in parts]
        if cleaned_parts and cleaned_parts[0].lower() in PERSON_TITLE_TOKENS:
            # Preserve title token, mutate only the actual name.
            mutated_parts = [parts[0]] + [_safe_mutate_token(part) for part in parts[1:]]
        else:
            mutated_parts = [_safe_mutate_token(part) for part in parts]
        return " ".join(mutated_parts)

    return NAME_PATTERN.sub(_replace, headline)
