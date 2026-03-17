"""Audio feature extraction using Essentia.

Enriches beat/bar metadata with musical features: key, energy, spectral
centroid, section boundaries, and vocal/instrumental classification.
"""

import json
import logging
import urllib.request
from pathlib import Path

import numpy as np

from mashup.models import BarFeatures, Section, TrackBeats, TrackFeatures

logger = logging.getLogger("mashup.feature_extraction")

SAMPLE_RATE = 44100
TF_SAMPLE_RATE = 16000

# Essentia TF model URLs and filenames
_MODEL_DIR = Path(__file__).parent / "models"
_VGGISH_EMBED = "audioset-vggish-3.pb"
_VOICE_CLASSIFY = "voice_instrumental-audioset-vggish-1.pb"
_VOICE_META = "voice_instrumental-audioset-vggish-1.json"
_MODEL_URLS = {
    _VGGISH_EMBED: "https://essentia.upf.edu/models/feature-extractors/vggish/audioset-vggish-3.pb",
    _VOICE_CLASSIFY: "https://essentia.upf.edu/models/classification-heads/voice_instrumental/voice_instrumental-audioset-vggish-1.pb",
    _VOICE_META: "https://essentia.upf.edu/models/classification-heads/voice_instrumental/voice_instrumental-audioset-vggish-1.json",
}


def _ensure_models() -> None:
    """Download TF model files if not already present."""
    _MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for filename, url in _MODEL_URLS.items():
        path = _MODEL_DIR / filename
        if not path.exists():
            logger.info("Downloading model %s", filename)
            urllib.request.urlretrieve(url, path)  # noqa: S310
            logger.info("Downloaded %s (%.1f MB)", filename, path.stat().st_size / 1e6)


def _load_audio(audio_path: Path, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Load audio as mono float32 array at the given sample rate."""
    from essentia.standard import MonoLoader

    loader = MonoLoader(filename=str(audio_path), sampleRate=sample_rate)
    return loader()


def _extract_bar_features(
    audio: np.ndarray, downbeats: list[float]
) -> list[BarFeatures]:
    """Compute key, energy, and spectral centroid for each bar."""
    from essentia.standard import Centroid, KeyExtractor, RMS, Spectrum, Windowing

    key_extractor = KeyExtractor(profileType="bgate", sampleRate=SAMPLE_RATE)
    rms = RMS()
    windowing = Windowing(type="hann")
    spectrum = Spectrum()
    centroid = Centroid(range=SAMPLE_RATE / 2)

    bars: list[BarFeatures] = []
    for i in range(len(downbeats) - 1):
        start_t = downbeats[i]
        end_t = downbeats[i + 1]
        start_s = int(start_t * SAMPLE_RATE)
        end_s = int(end_t * SAMPLE_RATE)
        segment = audio[start_s:end_s]

        if len(segment) < 2048:
            logger.debug("Bar %d too short (%d samples), skipping", i, len(segment))
            continue

        # Key detection
        key, scale, _ = key_extractor(segment)

        # RMS energy
        energy = float(rms(segment))

        # Spectral centroid (average over frames)
        centroid_values = []
        frame_size = 2048
        hop_size = 512
        for start in range(0, len(segment) - frame_size, hop_size):
            frame = segment[start : start + frame_size]
            spec = spectrum(windowing(frame))
            centroid_values.append(float(centroid(spec)))
        avg_centroid = float(np.mean(centroid_values)) if centroid_values else 0.0

        bars.append(
            BarFeatures(
                start=round(start_t, 4),
                end=round(end_t, 4),
                key=key,
                scale=scale,
                energy=round(energy, 6),
                spectral_centroid=round(avg_centroid, 6),
            )
        )

    logger.info("Extracted features for %d bars", len(bars))
    return bars


def _detect_sections(
    audio: np.ndarray, bars: list[BarFeatures]
) -> list[Section]:
    """Detect structural sections using MFCC-based segmentation, snapped to bar edges.

    Uses Essentia's SBic (Bayesian Information Criterion) for boundary detection,
    then snaps boundaries to the nearest bar edge.
    """
    from essentia.standard import FrameGenerator, MFCC, SBic, Spectrum, Windowing

    if len(bars) < 2:
        logger.warning("Not enough bars for section detection")
        return []

    # Extract MFCC features per frame
    windowing = Windowing(type="blackmanharris62")
    spectrum = Spectrum()
    mfcc = MFCC(numberCoefficients=13)

    frame_size = 2048
    hop_size = 512

    mfcc_features = []
    for frame in FrameGenerator(audio, frameSize=frame_size, hopSize=hop_size):
        _, mfcc_coeffs = mfcc(spectrum(windowing(frame)))
        mfcc_features.append(mfcc_coeffs)

    features = np.array(mfcc_features)
    logger.debug("MFCC feature matrix: %s", features.shape)

    # SBic expects (n_features, n_frames) — transpose from (n_frames, n_features)
    features_t = features.T

    # Run SBic segmentation
    sbic = SBic(size1=300, inc1=60, size2=200, inc2=20, cpw=1.5, minLength=10)
    boundaries = sbic(features_t)

    # Convert frame indices to seconds
    boundary_times = sorted(
        {float(idx) * hop_size / SAMPLE_RATE for idx in boundaries}
    )
    logger.debug("Raw SBic boundaries (seconds): %s", boundary_times)

    # Snap each boundary to nearest bar edge
    bar_edges = [b.start for b in bars] + [bars[-1].end]
    snapped = []
    for bt in boundary_times:
        closest = min(bar_edges, key=lambda e: abs(e - bt))
        if closest not in snapped:
            snapped.append(closest)
    snapped.sort()

    # Ensure first and last bar edges are included
    track_start = bars[0].start
    track_end = bars[-1].end
    if not snapped or snapped[0] != track_start:
        snapped.insert(0, track_start)
    if snapped[-1] != track_end:
        snapped.append(track_end)

    # Build sections from consecutive boundary pairs
    sections: list[Section] = []
    for i in range(len(snapped) - 1):
        start_t = snapped[i]
        end_t = snapped[i + 1]
        # Collect bar features within this section
        section_bars = [b for b in bars if b.start >= start_t and b.end <= end_t]
        mean_energy = (
            float(np.mean([b.energy for b in section_bars])) if section_bars else 0.0
        )
        mean_centroid = (
            float(np.mean([b.spectral_centroid for b in section_bars]))
            if section_bars
            else 0.0
        )
        sections.append(
            Section(
                start=round(start_t, 4),
                end=round(end_t, 4),
                label=f"section_{i + 1}",
                is_vocal=False,  # filled in by vocal detection step
                mean_energy=round(mean_energy, 6),
                mean_spectral_centroid=round(mean_centroid, 6),
            )
        )

    logger.info("Detected %d sections", len(sections))
    return sections


def _classify_vocals(
    audio_path: Path, sections: list[Section]
) -> list[Section]:
    """Classify each section as vocal or instrumental using VGGish TF model."""
    from essentia.standard import (
        MonoLoader,
        TensorflowPredict2D,
        TensorflowPredictVGGish,
    )

    _ensure_models()

    # Load at 16 kHz for TF models
    audio_16k = MonoLoader(
        filename=str(audio_path), sampleRate=TF_SAMPLE_RATE, resampleQuality=4
    )()

    embedding_model = TensorflowPredictVGGish(
        graphFilename=str(_MODEL_DIR / _VGGISH_EMBED),
        output="model/vggish/embeddings",
    )
    classifier = TensorflowPredict2D(
        graphFilename=str(_MODEL_DIR / _VOICE_CLASSIFY),
        output="model/Softmax",
    )

    # Load class labels
    with open(_MODEL_DIR / _VOICE_META) as f:
        meta = json.load(f)
    classes = meta["classes"]
    voice_idx = classes.index("voice")
    logger.debug("Voice class index: %d, classes: %s", voice_idx, classes)

    for section in sections:
        start_s = int(section.start * TF_SAMPLE_RATE)
        end_s = int(section.end * TF_SAMPLE_RATE)
        segment = audio_16k[start_s:end_s]

        if len(segment) < TF_SAMPLE_RATE:
            # Less than 1 second — too short for reliable classification
            logger.debug(
                "Section %s too short for vocal classification, defaulting to instrumental",
                section.label,
            )
            continue

        embeddings = embedding_model(segment)
        predictions = classifier(embeddings)
        avg_pred = predictions.mean(axis=0)
        is_vocal = bool(avg_pred[voice_idx] > 0.5)
        section.is_vocal = is_vocal
        logger.debug(
            "Section %s: voice=%.2f instrumental=%.2f -> %s",
            section.label,
            avg_pred[voice_idx],
            avg_pred[1 - voice_idx],
            "vocal" if is_vocal else "instrumental",
        )

    vocal_count = sum(1 for s in sections if s.is_vocal)
    logger.info(
        "Vocal classification: %d/%d sections contain vocals",
        vocal_count,
        len(sections),
    )
    return sections


def extract_features(audio_path: Path, beats_path: Path) -> TrackFeatures:
    """Run full feature extraction on a track.

    Args:
        audio_path: Path to the audio file (FLAC).
        beats_path: Path to the .beats.json file from beat detection.

    Returns:
        TrackFeatures with per-bar features, sections, and vocal classification.
    """
    logger.info("Starting feature extraction for %s", audio_path.name)

    # Load beat metadata
    beats = TrackBeats.model_validate_json(beats_path.read_text())
    logger.info(
        "Loaded beats: bpm=%.2f downbeats=%d time_signature=%d/4",
        beats.bpm,
        len(beats.downbeats),
        beats.time_signature,
    )

    # Load audio at 44.1 kHz for DSP features
    logger.info("Loading audio at %d Hz", SAMPLE_RATE)
    audio = _load_audio(audio_path, SAMPLE_RATE)
    logger.info("Audio loaded: %.1f seconds, %d samples", len(audio) / SAMPLE_RATE, len(audio))

    # Global key detection
    from essentia.standard import KeyExtractor

    key_extractor = KeyExtractor(profileType="bgate", sampleRate=SAMPLE_RATE)
    global_key, global_scale, _ = key_extractor(audio)
    logger.info("Global key: %s %s", global_key, global_scale)

    # Global energy
    from essentia.standard import RMS

    global_energy = float(RMS()(audio))
    logger.info("Global RMS energy: %.6f", global_energy)

    # Per-bar features
    bars = _extract_bar_features(audio, beats.downbeats)

    # Section detection
    sections = _detect_sections(audio, bars)

    # Vocal classification per section
    if sections:
        sections = _classify_vocals(audio_path, sections)

    result = TrackFeatures(
        audio_file=audio_path.name,
        bpm=beats.bpm,
        time_signature=beats.time_signature,
        global_key=global_key,
        global_scale=global_scale,
        global_energy=round(global_energy, 6),
        bars=bars,
        sections=sections,
    )
    logger.info(
        "Feature extraction complete: key=%s %s, %d bars, %d sections",
        global_key,
        global_scale,
        len(bars),
        len(sections),
    )
    return result
