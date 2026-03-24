"""Creative concept and prompt generation."""

import json
from typing import Sequence, Tuple

from openai import OpenAI


def _extract_json(text: str) -> dict:
    """Extract and parse the first JSON object found in a model response."""
    cleaned = text.strip()

    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json\n", "", 1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


def generate_collage_concept_and_prompt(
    client: OpenAI,
    model: str,
    headline: str,
    persona_context: str = "",
    story_context: str = "",
    visual_cues: Sequence[str] | None = None,
) -> Tuple[str, str]:
    """Generate a surreal collage concept and image prompt from a headline."""
    visual_cue_lines = ""
    if visual_cues:
        visual_cue_lines = "\n".join(f"- {cue}" for cue in visual_cues if cue.strip())

    context_block = ""
    if story_context.strip() or visual_cue_lines:
        context_block = (
            "\nStory-grounding context (use as anchors for scene details):\n"
            f"- Story context: {story_context.strip() or headline}\n"
        )
        if visual_cue_lines:
            context_block += f"- Visual cues from source metadata/images:\n{visual_cue_lines}\n"

    base_instruction = (
        "You are an office photocopier that secretly makes satirical collage art about the news.\n\n"
        "Given the following headline, invent an avant-garde surrealist collage artwork concept involving "
        "robots, office equipment, and nerd-culture references.\n"
        "Prioritize dream logic over literal illustration.\n"
        "The visual direction must strongly look like torn paper collage mixed with photoreal textures, "
        "cut edges, ripped magazine clippings, layered scans, glue marks, and imperfect photocopy noise.\n"
        "The concept should feel visionary, uncanny, and socially observant, with surreal juxtapositions, "
        "impossible scale shifts, symbolic props, and strange narrative tension.\n"
        "Use satirical social commentary and absurd humor, with occasional geek references: gaming culture, "
        "sci-fi aesthetics, and robot archetypes.\n"
        "If persona context includes seasonal tone tags or surreal intensity, treat them as hard style targets.\n"
        "Avoid mainstream celebrity gossip or literal news reenactments.\n"
        "Important: retain clear relation to the underlying news story.\n"
        "Include at least two concrete anchors from story-grounding context (for example: location, "
        "setting, object, event type, or public-space detail), then transform them surrealistically.\n"
        "Use source visual cues as inspiration only; reinterpret, distort, or morph them rather than copying.\n"
        "Do not depict or reference real people, celebrities, politicians, or public figures.\n"
        "Do not use trademarked characters, franchise names, logos, or brand names.\n"
        "Do not include readable text, headlines, letters, titles, or watermarks inside the image.\n"
        "Treat any names in the headline as fictional aliases, not real people.\n"
        "If the headline implies a real person or brand, convert it into a fictional archetype.\n\n"
        f"{context_block}"
        f"Headline: {headline}\n\n"
        "Return a JSON object with exactly these keys:\n"
        "- collage_concept\n"
        "- image_prompt\n\n"
        "The image_prompt must be specific about materials and composition: torn paper, collage seams, "
        "mixed media layers, halftone print texture, and satirical editorial tone.\n"
        "The image_prompt should explicitly include:\n"
        "- one dreamlike environment\n"
        "- one impossible or paradoxical visual element\n"
        "- one symbolic object representing social commentary"
    )
    instruction = base_instruction
    if persona_context.strip():
        instruction = f"{persona_context.strip()}\n\n{base_instruction}"

    try:
        response = client.responses.create(model=model, input=instruction, temperature=1.15)
    except Exception as exc:
        raise RuntimeError(f"Failed to generate collage concept: {exc}") from exc

    text = (response.output_text or "").strip()
    if not text:
        raise RuntimeError("OpenAI returned an empty response for collage concept generation.")

    try:
        data = _extract_json(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Could not parse concept response as JSON. "
            f"Raw response: {text[:200]}"
        ) from exc

    collage_concept = str(data.get("collage_concept", "")).strip()
    image_prompt = str(data.get("image_prompt", "")).strip()
    if not collage_concept or not image_prompt:
        raise RuntimeError("OpenAI response is missing collage_concept or image_prompt.")

    return collage_concept, image_prompt
