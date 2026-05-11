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
        "The visual direction must strongly look like a handmade torn-paper mixed-media photomontage, not a "
        "single unified illustration or painting.\n"
        "Treat the image like it was physically built from ripped fragments taken from different magazines, "
        "newspapers, catalogues, adverts, office documents, comics, diagrams, and photocopied scraps.\n"
        "A substantial portion of the collage should come from photorealistic printed clippings of real-looking subjects, "
        "objects, interiors, machinery, and textures torn from magazines and newspapers, mixed with only some illustrated or "
        "diagrammatic fragments.\n"
        "Do not let every element become drawn, painted, airbrushed, or softly illustrated; the result should clearly feel "
        "assembled from photographed source material plus a smaller amount of graphic print matter.\n"
        "If robots, office machines, hands, interiors, or objects appear, they should look like photographs that were printed "
        "in magazines or newspapers and then torn out, not like fresh digital illustrations.\n"
        "Keep the photographic fragments photorealistic but visibly printed, with halftone dots, offset printing texture, "
        "cheap paper reproduction, ink spread, faded blacks, and uneven colour registration.\n"
        "Each fragment should feel visibly different in print quality, colour balance, paper stock, ink density, "
        "halftone pattern, and age, with mismatched tones that do not blend into one smooth palette.\n"
        "Emphasize hand-torn edges, overlapping paper layers, rough cut shapes, visible glue marks, taped seams, "
        "photocopied dirt, misregistration, and imperfect analogue assembly.\n"
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
        "mixed media layers, visibly different source fragments, halftone print texture, photorealistic printed clippings, and satirical editorial tone.\n"
        "It should explicitly push for varied source materials and contrasting print looks rather than a single "
        "harmonized colour theme.\n"
        "The image_prompt should explicitly include:\n"
        "- one dreamlike environment\n"
        "- one impossible or paradoxical visual element\n"
        "- one symbolic object representing social commentary\n"
        "- at least three distinct paper/source types with noticeably different visual treatment\n"
        "- at least one clearly photographic clipping and one clearly printed or drawn fragment\n"
        "- an explicit instruction that the main subjects should look like torn printed photographs, not illustrations"
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
