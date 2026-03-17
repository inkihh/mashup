import json
import logging
from pathlib import Path

from jinja2 import Template

from mashup.ai import chat
from mashup.models import TrackSelection

logger = logging.getLogger("mashup.track_selection")

PROMPT_PATH = Path(__file__).parent / "prompts" / "track_selection.md"


def build_prompt(
    *,
    seed_artist: str | None = None,
    seed_title: str | None = None,
    genre: str | None = None,
    mood: str | None = None,
    era: str | None = None,
) -> str:
    template = Template(PROMPT_PATH.read_text())
    return template.render(
        seed_artist=seed_artist,
        seed_title=seed_title,
        genre=genre,
        mood=mood,
        era=era,
    )


def _extract_json(raw: str) -> dict:
    """Extract a JSON object from a string that may contain extra text."""
    start = raw.index("{")
    depth, end = 0, start
    for i, ch in enumerate(raw[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return json.loads(raw[start:end])


def select_tracks(
    *,
    seed_artist: str | None = None,
    seed_title: str | None = None,
    genre: str | None = None,
    mood: str | None = None,
    era: str | None = None,
) -> TrackSelection:
    prompt = build_prompt(
        seed_artist=seed_artist,
        seed_title=seed_title,
        genre=genre,
        mood=mood,
        era=era,
    )
    logger.debug("Rendered prompt (%d chars)", len(prompt))

    raw = chat(prompt, task="select", web_search=True)
    logger.debug("Raw response: %s", raw)

    data = _extract_json(raw)
    result = TrackSelection.model_validate(data)

    logger.info("Selected: %s - %s  x  %s - %s",
                result.track_a.artist, result.track_a.title,
                result.track_b.artist, result.track_b.title)
    return result
