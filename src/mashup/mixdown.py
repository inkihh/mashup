import logging
from pathlib import Path

import numpy as np
import pedalboard
import soundfile as sf
from pedalboard.io import WriteableAudioFile

from mashup.models import (
    Compressor,
    Delay,
    HighPass,
    LowPass,
    MixPlan,
    MixSlice,
    MixTrackRole,
    Reverb,
    TrackBeats,
    TrackEffect,
)

logger = logging.getLogger("mashup.mixdown")

# Short crossfade between adjacent beat slices to avoid clicks (in samples)
BEAT_SPLICE_XFADE = 128

# Fade in/out applied to every slice to avoid clicks at boundaries (in samples)
SLICE_FADE_SAMPLES = 256


def _build_effect(effect: TrackEffect) -> pedalboard.Plugin:
    match effect:
        case HighPass(freq_hz=freq):
            return pedalboard.HighpassFilter(cutoff_frequency_hz=freq)
        case LowPass(freq_hz=freq):
            return pedalboard.LowpassFilter(cutoff_frequency_hz=freq)
        case Reverb(wet_ratio=wet):
            return pedalboard.Reverb(wet_level=wet)
        case Delay(delay_ms=ms, feedback=fb):
            return pedalboard.Delay(delay_seconds=ms / 1000.0, feedback=fb)
        case Compressor(threshold_db=thresh, ratio=ratio):
            return pedalboard.Compressor(threshold_db=thresh, ratio=ratio)
        case _:
            raise ValueError(f"Unknown effect type: {effect}")


def _apply_effects(audio: np.ndarray, sr: int, role: MixTrackRole) -> np.ndarray:
    from mashup.ai import get_enabled_effects

    if role.gain_db != 0.0:
        audio = audio * (10.0 ** (role.gain_db / 20.0))

    # Filter out disabled effects
    enabled = set(get_enabled_effects())
    effects = [e for e in role.effects if e.type in enabled]

    if effects:
        plugins = [_build_effect(e) for e in effects]
        board = pedalboard.Pedalboard(plugins)
        was_1d = audio.ndim == 1
        if was_1d:
            audio = audio[np.newaxis, :]
        else:
            audio = audio.T
        audio = board(audio.astype(np.float32), sr)
        if was_1d:
            audio = audio[0]
        else:
            audio = audio.T

    return audio


def _apply_slice_fades(audio: np.ndarray, fade_samples: int) -> np.ndarray:
    """Apply short fade-in and fade-out to a slice to prevent clicks."""
    if audio.shape[0] < fade_samples * 2:
        return audio
    audio = audio.copy()
    fade_in = np.linspace(0.0, 1.0, fade_samples, dtype=np.float64)
    fade_out = np.linspace(1.0, 0.0, fade_samples, dtype=np.float64)
    if audio.ndim == 2:
        audio[:fade_samples] *= fade_in[:, np.newaxis]
        audio[-fade_samples:] *= fade_out[:, np.newaxis]
    else:
        audio[:fade_samples] *= fade_in
        audio[-fade_samples:] *= fade_out
    return audio


def _scale_beats(beats: list[float], original_bpm: float, target_bpm: int) -> np.ndarray:
    ratio = original_bpm / target_bpm
    return np.array([b * ratio for b in beats])


def _reassemble_on_grid(
    audio: np.ndarray,
    sr: int,
    beat_times: np.ndarray,
    source_start: float,
    source_end: float,
    grid_spacing: float,
) -> np.ndarray:
    """Cut audio at beat positions and reassemble on a perfect grid."""
    mask = (beat_times >= source_start - 0.001) & (beat_times < source_end + 0.001)
    beats = beat_times[mask]

    if len(beats) == 0:
        s = int(round(source_start * sr))
        e = int(round(source_end * sr))
        s = max(0, min(s, len(audio)))
        e = max(s, min(e, len(audio)))
        return audio[s:e]

    grid_samples = int(round(grid_spacing * sr))
    n_beats = len(beats)
    total_samples = n_beats * grid_samples
    channels = audio.shape[1] if audio.ndim > 1 else 1
    result = np.zeros((total_samples, channels), dtype=np.float64)

    xfade = min(BEAT_SPLICE_XFADE, grid_samples // 4)
    audio_len = len(audio)

    for i in range(n_beats):
        slice_start = int(round(beats[i] * sr))
        slice_end = slice_start + grid_samples + xfade
        slice_start = max(0, min(slice_start, audio_len))
        slice_end = max(slice_start, min(slice_end, audio_len))
        beat_slice = audio[slice_start:slice_end].copy()

        if beat_slice.ndim == 1:
            beat_slice = beat_slice[:, np.newaxis]

        grid_pos = i * grid_samples
        usable = min(beat_slice.shape[0], total_samples - grid_pos)
        if usable <= 0:
            continue

        if i > 0 and xfade > 0 and usable > xfade:
            fade_in = np.linspace(0.0, 1.0, xfade, dtype=np.float64)[:, np.newaxis]
            beat_slice[:xfade] *= fade_in
            result[grid_pos:grid_pos + xfade] *= (1.0 - fade_in[:min(xfade, usable)])

        result[grid_pos:grid_pos + usable] += beat_slice[:usable]

    return result


def _process_role(
    role: MixTrackRole,
    audio: np.ndarray,
    beats: np.ndarray,
    sr: int,
    grid_spacing: float,
    channels: int,
) -> np.ndarray:
    """Process one track's role in a slice: beat-grid reassemble, effects, fade."""
    reassembled = _reassemble_on_grid(audio, sr, beats, role.source_start, role.source_end, grid_spacing)
    if reassembled.size == 0:
        return np.zeros((0, channels), dtype=np.float64)

    processed = _apply_effects(reassembled, sr, role)
    if processed.ndim == 1:
        processed = processed[:, np.newaxis]
    if processed.shape[1] != channels:
        processed = np.broadcast_to(processed, (processed.shape[0], channels)).copy()

    return processed


def mixdown(project_dir: Path) -> list[Path]:
    """Execute a mix plan and export the final mashup.

    Slices can layer both tracks (vocals over instrumental) or play one solo.
    Beat-grid reassembly keeps everything locked to tempo.
    """
    mix_plan_path = project_dir / "data" / "mix_plan.json"
    if not mix_plan_path.exists():
        raise FileNotFoundError(f"Mix plan not found: {mix_plan_path}")

    mix_plan = MixPlan.model_validate_json(mix_plan_path.read_text())
    logger.info("Loaded mix plan: target_bpm=%d, %d slices", mix_plan.target_bpm, len(mix_plan.slices))

    from mashup.audio_download import track_filenames
    from mashup.models import TrackSelection

    selection_path = project_dir / "track_selection.json"
    if not selection_path.exists():
        raise FileNotFoundError(f"No track_selection.json found in {project_dir}")
    selection = TrackSelection.model_validate_json(selection_path.read_text())
    name_a, name_b = track_filenames(selection)

    prepared_dir = project_dir / "data" / "prepared"
    file_a = prepared_dir / name_a
    file_b = prepared_dir / name_b
    for f in [file_a, file_b]:
        if not f.exists():
            raise FileNotFoundError(f"Prepared audio not found: {f}")

    audio_a, sr_a = sf.read(file_a, dtype="float64")
    audio_b, sr_b = sf.read(file_b, dtype="float64")
    if sr_a != sr_b:
        raise ValueError(f"Sample rate mismatch: {file_a.name}={sr_a}, {file_b.name}={sr_b}")
    sr = sr_a
    logger.info("Loaded audio: A=%s (%.1fs), B=%s (%.1fs), sr=%d",
                file_a.name, len(audio_a) / sr, file_b.name, len(audio_b) / sr, sr)

    # Load and scale beat positions
    beats_dir = project_dir / "data" / "beats"
    beats_a = TrackBeats.model_validate_json((beats_dir / name_a.replace(".flac", ".beats.json")).read_text())
    beats_b = TrackBeats.model_validate_json((beats_dir / name_b.replace(".flac", ".beats.json")).read_text())

    scaled_beats_a = _scale_beats(beats_a.beats, beats_a.bpm, mix_plan.target_bpm)
    scaled_beats_b = _scale_beats(beats_b.beats, beats_b.bpm, mix_plan.target_bpm)
    logger.info("Scaled beats: A=%d, B=%d", len(scaled_beats_a), len(scaled_beats_b))

    grid_spacing = 60.0 / mix_plan.target_bpm
    logger.info("Beat grid spacing: %.4fs (%.1f BPM)", grid_spacing, mix_plan.target_bpm)

    # Ensure 2D
    if audio_a.ndim == 1:
        audio_a = audio_a[:, np.newaxis]
    if audio_b.ndim == 1:
        audio_b = audio_b[:, np.newaxis]
    channels = max(audio_a.shape[1], audio_b.shape[1])

    # Process each slice
    slice_audios: list[np.ndarray] = []
    for i, s in enumerate(mix_plan.slices):
        layers: list[np.ndarray] = []

        if s.track_a is not None:
            a = _process_role(s.track_a, audio_a, scaled_beats_a, sr, grid_spacing, channels)
            if a.size > 0:
                layers.append(a)
                logger.info("Slice %d: track_a %.1f–%.1fs", i, s.track_a.source_start, s.track_a.source_end)

        if s.track_b is not None:
            b = _process_role(s.track_b, audio_b, scaled_beats_b, sr, grid_spacing, channels)
            if b.size > 0:
                layers.append(b)
                logger.info("Slice %d: track_b %.1f–%.1fs", i, s.track_b.source_start, s.track_b.source_end)

        if not layers:
            logger.warning("Slice %d produced no audio", i)
            continue

        # Layer: pad to same length and sum
        max_len = max(l.shape[0] for l in layers)
        layered = np.zeros((max_len, channels), dtype=np.float64)
        for l in layers:
            layered[:l.shape[0]] += l

        # Apply short fade in/out to avoid clicks at slice boundaries
        layered = _apply_slice_fades(layered, SLICE_FADE_SAMPLES)
        slice_audios.append(layered)

    if not slice_audios:
        raise RuntimeError("Mixdown produced no audio")

    output = np.concatenate(slice_audios, axis=0)

    # Peak normalization to -1 dBFS
    peak = np.max(np.abs(output))
    if peak > 0:
        target_peak = 10.0 ** (-1.0 / 20.0)
        output = output * (target_peak / peak)
        logger.info("Normalized: peak %.4f → %.4f (-1 dBFS)", peak, target_peak)

    output = output.astype(np.float32)

    # Export
    output_dir = project_dir / "data" / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    project_name = project_dir.name
    flac_path = output_dir / f"{project_name}.flac"
    mp3_path = output_dir / f"{project_name}.mp3"

    sf.write(str(flac_path), output, sr)
    logger.info("Exported FLAC: %s", flac_path)

    with WriteableAudioFile(str(mp3_path), sr, channels, quality="192k") as f:
        f.write(output.T)
    logger.info("Exported MP3: %s", mp3_path)

    return [flac_path, mp3_path]
