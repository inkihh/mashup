import json
import logging
from pathlib import Path

from jinja2 import Template

from mashup.ai import chat, get_enabled_effects
from mashup.models import MixPlan, TrackFeatures, TrackSelection

logger = logging.getLogger("mashup.mix_planning")

PROMPT_PATH = Path(__file__).parent / "prompts" / "mix_planning.md"


def build_mix_prompt(
    selection: TrackSelection,
    features_a: TrackFeatures,
    features_b: TrackFeatures,
) -> str:
    template = Template(PROMPT_PATH.read_text())
    return template.render(
        track_a=selection.track_a,
        track_b=selection.track_b,
        features_a=features_a,
        features_b=features_b,
        rationale=selection.rationale,
        enabled_effects=get_enabled_effects(),
    )


def _fixup_effects(effects: list) -> None:
    """Normalize effect field names from AI model variants."""
    aliases = {
        "frequency": "freq_hz",
        "cutoff": "freq_hz",
        "cutoff_hz": "freq_hz",
        "wet": "wet_ratio",
        "wet_level": "wet_ratio",
        "delay_time": "delay_ms",
        "threshold": "threshold_db",
    }
    for effect in effects:
        if not isinstance(effect, dict):
            continue
        for old_key, new_key in aliases.items():
            if old_key in effect and new_key not in effect:
                effect[new_key] = effect.pop(old_key)


def _fixup_mix_plan(data: dict) -> None:
    """Normalize field names throughout a mix plan dict."""
    for s in data.get("slices", []):
        for track_key in ("track_a", "track_b"):
            role = s.get(track_key)
            if isinstance(role, dict) and "effects" in role:
                _fixup_effects(role["effects"])


def plan_mix(project_dir: Path) -> MixPlan:
    selection_path = project_dir / "track_selection.json"
    if not selection_path.exists():
        raise FileNotFoundError(f"No track_selection.json found in {project_dir}")

    selection = TrackSelection.model_validate_json(selection_path.read_text())
    logger.info("Loaded track selection: %s x %s",
                selection.track_a.artist, selection.track_b.artist)

    from mashup.audio_download import track_filenames

    features_dir = project_dir / "data" / "features"
    name_a, name_b = track_filenames(selection)
    feat_path_a = features_dir / (name_a.replace(".flac", ".features.json"))
    feat_path_b = features_dir / (name_b.replace(".flac", ".features.json"))

    if not feat_path_a.exists() or not feat_path_b.exists():
        raise FileNotFoundError(
            f"Expected feature files {feat_path_a.name} and {feat_path_b.name} in {features_dir}"
        )

    features_a = TrackFeatures.model_validate_json(feat_path_a.read_text())
    features_b = TrackFeatures.model_validate_json(feat_path_b.read_text())
    logger.info("Loaded features: %s (%d sections), %s (%d sections)",
                features_a.audio_file, len(features_a.sections),
                features_b.audio_file, len(features_b.sections))

    prompt = build_mix_prompt(selection, features_a, features_b)
    logger.debug("Rendered mix planning prompt (%d chars)", len(prompt))

    raw = chat(prompt, task="plan")
    logger.debug("Raw response: %s", raw)

    # Extract JSON even if model adds extra text
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
    data = json.loads(raw[start:end])

    # Normalize common field name variants from different AI models
    _fixup_mix_plan(data)

    result = MixPlan.model_validate(data)
    logger.info("Mix plan: %d slices, target_bpm=%d, pitch_a=%+d, pitch_b=%+d",
                len(result.slices), result.target_bpm,
                result.track_a_pitch_shift_semitones,
                result.track_b_pitch_shift_semitones)
    return result
