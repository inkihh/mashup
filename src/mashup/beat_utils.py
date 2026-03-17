"""BPM compatibility utilities."""

import logging

logger = logging.getLogger("mashup.beat_utils")

# Maximum BPM difference (%) before tracks are considered incompatible
BPM_THRESHOLD_PCT = 15


def bpm_diff_pct(a: float, b: float) -> float:
    """Calculate BPM difference as a percentage of the slower tempo."""
    return abs(a - b) / min(a, b) * 100


def check_bpm_compatibility(bpm_a: float, bpm_b: float) -> None:
    """Check if two BPMs are compatible, considering half/double-time.

    Compares the raw BPMs first, then tries halving the faster one
    to see if they match at half-time. Raises RuntimeError if
    no reasonable match is found.
    """
    # Direct comparison
    diff = bpm_diff_pct(bpm_a, bpm_b)
    if diff <= BPM_THRESHOLD_PCT:
        logger.info(
            "BPM compatible: %.1f vs %.1f (%.1f%% apart)",
            bpm_a, bpm_b, diff,
        )
        return

    # Try half-time: halve the faster BPM
    fast, slow = max(bpm_a, bpm_b), min(bpm_a, bpm_b)
    half_diff = bpm_diff_pct(fast / 2, slow)
    if half_diff <= BPM_THRESHOLD_PCT:
        logger.info(
            "BPM compatible at half-time: %.1f/2=%.1f vs %.1f (%.1f%% apart)",
            fast, fast / 2, slow, half_diff,
        )
        return

    # Try double-time: double the slower BPM
    double_diff = bpm_diff_pct(slow * 2, fast)
    if double_diff <= BPM_THRESHOLD_PCT:
        logger.info(
            "BPM compatible at double-time: %.1f*2=%.1f vs %.1f (%.1f%% apart)",
            slow, slow * 2, fast, double_diff,
        )
        return

    raise RuntimeError(
        f"Detected BPMs are {diff:.1f}% apart "
        f"({bpm_a:.1f} vs {bpm_b:.1f}). "
        f"These tracks may not work well together. "
        f"Consider re-running track selection."
    )
