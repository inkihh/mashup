import logging
import shutil
from pathlib import Path

import pyrubberband as pyrb
import soundfile as sf

from mashup.models import MixPlan, TrackBeats, TrackSelection

logger = logging.getLogger("mashup.time_stretch")


def prepare_track(
    audio_path: Path,
    original_bpm: float,
    target_bpm: int,
    pitch_shift_semitones: int,
    output_path: Path,
) -> Path:
    """Time-stretch and pitch-shift a single track.

    If no processing is needed (BPM matches and pitch shift is 0),
    copies the file as-is.
    """
    needs_stretch = abs(original_bpm - target_bpm) > 0.01
    needs_pitch = pitch_shift_semitones != 0

    if not needs_stretch and not needs_pitch:
        logger.info("No processing needed for %s, copying as-is", audio_path.name)
        shutil.copy2(audio_path, output_path)
        return output_path

    logger.info(
        "Processing %s: %.2f BPM → %d BPM, pitch shift %+d semitones",
        audio_path.name, original_bpm, target_bpm, pitch_shift_semitones,
    )

    audio, sr = sf.read(audio_path)
    logger.debug("Loaded audio: %d samples, %d Hz, %d channels", len(audio), sr, audio.ndim)

    if needs_stretch:
        stretch_ratio = target_bpm / original_bpm
        logger.info("Time-stretching by ratio %.4f", stretch_ratio)
        audio = pyrb.time_stretch(audio, sr, stretch_ratio)

    if needs_pitch:
        logger.info("Pitch-shifting by %+d semitones (formant-preserving)", pitch_shift_semitones)
        audio = pyrb.pitch_shift(audio, sr, pitch_shift_semitones, rbargs={"--formant": ""})

    sf.write(str(output_path), audio, sr)
    logger.info("Saved prepared audio to %s", output_path)
    return output_path


def prepare_tracks(project_dir: Path) -> list[Path]:
    """Prepare all tracks in a project directory according to the mix plan."""
    mix_plan_path = project_dir / "data" / "mix_plan.json"
    if not mix_plan_path.exists():
        raise FileNotFoundError(f"Mix plan not found: {mix_plan_path}")

    mix_plan = MixPlan.model_validate_json(mix_plan_path.read_text())
    logger.info("Loaded mix plan: target_bpm=%d", mix_plan.target_bpm)

    from mashup.audio_download import track_filenames

    selection_path = project_dir / "track_selection.json"
    if not selection_path.exists():
        raise FileNotFoundError(f"No track_selection.json found in {project_dir}")
    selection = TrackSelection.model_validate_json(selection_path.read_text())
    name_a, name_b = track_filenames(selection)

    input_dir = project_dir / "data" / "input"
    beats_dir = project_dir / "data" / "beats"
    prepared_dir = project_dir / "data" / "prepared"
    prepared_dir.mkdir(parents=True, exist_ok=True)

    flac_files = [input_dir / name_a, input_dir / name_b]
    for f in flac_files:
        if not f.exists():
            raise FileNotFoundError(f"Audio file not found: {f}")

    pitch_shifts = [
        mix_plan.track_a_pitch_shift_semitones,
        mix_plan.track_b_pitch_shift_semitones,
    ]
    labels = ["A", "B"]

    output_paths = []
    time_ratios = []
    for audio_path, pitch_shift, label in zip(flac_files, pitch_shifts, labels):
        beats_path = beats_dir / (audio_path.stem + ".beats.json")
        if not beats_path.exists():
            raise FileNotFoundError(f"Beats file not found: {beats_path}")

        beats = TrackBeats.model_validate_json(beats_path.read_text())
        output_path = prepared_dir / audio_path.name

        logger.info(
            "Track %s (%s): original BPM=%.2f, target BPM=%d, pitch shift=%+d",
            label, audio_path.name, beats.bpm, mix_plan.target_bpm, pitch_shift,
        )

        prepare_track(
            audio_path=audio_path,
            original_bpm=beats.bpm,
            target_bpm=mix_plan.target_bpm,
            pitch_shift_semitones=pitch_shift,
            output_path=output_path,
        )
        output_paths.append(output_path)
        time_ratios.append(beats.bpm / mix_plan.target_bpm)

    # Recalculate mix plan timestamps to match the time-stretched audio
    _adjust_mix_plan_timestamps(mix_plan, time_ratios[0], time_ratios[1])
    mix_plan_path.write_text(mix_plan.model_dump_json(indent=2))
    logger.info("Updated mix plan timestamps for prepared audio")

    return output_paths


def _adjust_mix_plan_timestamps(
    mix_plan: MixPlan,
    ratio_a: float,
    ratio_b: float,
) -> None:
    """Scale source_start/source_end in all slices by the time-stretch ratio.

    When a track is sped up (target BPM > original BPM), the audio gets shorter,
    so timestamps shrink by original_bpm / target_bpm.
    """
    for s in mix_plan.slices:
        if s.track_a is not None:
            s.track_a.source_start = round(s.track_a.source_start * ratio_a, 4)
            s.track_a.source_end = round(s.track_a.source_end * ratio_a, 4)
        if s.track_b is not None:
            s.track_b.source_start = round(s.track_b.source_start * ratio_b, 4)
            s.track_b.source_end = round(s.track_b.source_end * ratio_b, 4)
