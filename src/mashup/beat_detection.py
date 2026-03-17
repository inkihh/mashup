import logging
from collections import Counter
from pathlib import Path

import numpy as np
from beat_this.inference import File2Beats

from mashup.models import TrackBeats

logger = logging.getLogger("mashup.beat_detection")

_file2beats: File2Beats | None = None


def _get_model() -> File2Beats:
    """Lazy-load the beat_this model (downloads weights on first use)."""
    global _file2beats
    if _file2beats is None:
        logger.info("Loading beat_this model (first use — may download weights)")
        _file2beats = File2Beats()
        logger.info("beat_this model loaded")
    return _file2beats


def _essentia_bpm(audio_path: Path) -> float:
    """Estimate BPM using essentia's RhythmExtractor2013 as a cross-check."""
    import essentia.standard as es

    loader = es.MonoLoader(filename=str(audio_path), sampleRate=44100)
    audio = loader()
    rhythm = es.RhythmExtractor2013(method="multifeature")
    bpm, _ticks, confidence, _estimates, _intervals = rhythm(audio)
    logger.info("Essentia BPM: %.1f (confidence: %.3f)", bpm, confidence)
    return float(bpm)


def detect_beats(audio_path: Path) -> TrackBeats:
    """Run beat and downbeat detection on an audio file."""
    logger.info("Running beat detection on %s", audio_path.name)
    model = _get_model()
    beats, downbeats = model(audio_path)
    logger.debug("Raw results: %d beats, %d downbeats", len(beats), len(downbeats))

    # Primary BPM from beat_this median interval
    if len(beats) >= 2:
        intervals = np.diff(beats)
        median_interval = float(np.median(intervals))
        bpm_beats = 60.0 / median_interval if median_interval > 0 else 0.0
    else:
        bpm_beats = 0.0

    # Cross-check with essentia's global tempo estimator
    bpm_essentia = _essentia_bpm(audio_path)

    # Cross-reference beat_this and essentia BPM estimates.
    # When they agree, average them. When one is double the other, prefer
    # the slower value (the groove/feel BPM rather than subdivisions).
    # When they disagree entirely, average them as a best guess.
    bpm = bpm_essentia
    if bpm_beats > 0:
        ratio = bpm_beats / bpm_essentia
        if abs(ratio - 1.0) < 0.05:
            # They agree — average them
            bpm = (bpm_beats + bpm_essentia) / 2
            logger.info("BPM consensus: beat_this=%.1f, essentia=%.1f → %.1f", bpm_beats, bpm_essentia, bpm)
        elif abs(ratio - 0.5) < 0.1:
            # beat_this is half of essentia — prefer beat_this (groove BPM)
            bpm = bpm_beats
            logger.warning(
                "beat_this BPM (%.1f) is half essentia (%.1f) — using slower groove BPM",
                bpm_beats, bpm_essentia,
            )
        elif abs(ratio - 2.0) < 0.1:
            # beat_this is double of essentia — prefer essentia (groove BPM)
            bpm = bpm_essentia
            logger.warning(
                "beat_this BPM (%.1f) is double essentia (%.1f) — using slower groove BPM",
                bpm_beats, bpm_essentia,
            )
        else:
            bpm = (bpm_beats + bpm_essentia) / 2
            logger.warning(
                "BPM mismatch: beat_this=%.1f, essentia=%.1f (ratio=%.2f) — averaging to %.1f",
                bpm_beats, bpm_essentia, ratio, bpm,
            )

    # Infer time signature from beats between consecutive downbeats
    time_signature = _infer_time_signature(beats, downbeats)

    logger.info("Beat detection complete: bpm=%.2f beats=%d downbeats=%d ts=%d/4",
                bpm, len(beats), len(downbeats), time_signature)
    return TrackBeats(
        audio_file=audio_path.name,
        bpm=round(bpm, 2),
        beats=[round(float(b), 4) for b in beats],
        downbeats=[round(float(d), 4) for d in downbeats],
        time_signature=time_signature,
    )


def _infer_time_signature(
    beats: np.ndarray, downbeats: np.ndarray
) -> int:
    """Count beats between consecutive downbeats to infer time signature."""
    if len(downbeats) < 2 or len(beats) < 2:
        return 4  # default assumption

    counts: list[int] = []
    for i in range(len(downbeats) - 1):
        start, end = downbeats[i], downbeats[i + 1]
        n_beats = int(np.sum((beats >= start - 0.05) & (beats < end - 0.05)))
        if n_beats > 0:
            counts.append(n_beats)

    if not counts:
        return 4

    # Most common beats-per-bar
    most_common = Counter(counts).most_common(1)[0][0]
    return most_common
