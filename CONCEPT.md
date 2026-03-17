# Mashup - Automatic Music Mashup Generator

A Python CLI app that automatically creates music mashups by combining two tracks from different genres into a cohesive mix, using AI for creative decisions and audio DSP libraries for the actual processing.

## Pipeline

### 1. Track Selection (Claude API)

AI picks two tracks that work well together as a mashup:

- **Key compatibility** — same key or closely related keys (e.g., relative major/minor, parallel keys, dominant relationship)
- **BPM compatibility** — similar enough tempo that pitch-shifting stays within a reasonable range (e.g., within 10-15% BPM difference)
- **Genre contrast** — intentionally different genres that create an interesting mashup (e.g., hip-hop acapella over an electronic instrumental, jazz over drum & bass)
- Output: two track identifiers (artist + title) with rationale for why they'd work together

### 2. Audio Acquisition (yt-dlp)

- Automatically download the two tracks from YouTube using yt-dlp
- Search by artist + title, pick the best match (official audio/video, highest audio quality)
- Extract audio as FLAC (lossless, smaller than WAV) for processing downstream
- Fallback: user provides audio files manually if download fails or track isn't available

### 3. Beat & Bar Detection (beat_this)

Analyze both tracks using beat_this (CPJKU, ISMIR 2024 — successor to madmom) to extract timing metadata:

- Beat positions (timestamps)
- Bar boundaries (downbeats)
- Global BPM and time signature
- Output: JSON metadata file per track

### 4. Audio Feature Enrichment (Essentia)

Uses beat_this's beat/bar timestamps as the analysis grid — custom glue code slices the audio at bar boundaries and runs Essentia's algorithms on each segment.

Enrich the metadata with musical features:

- Key and scale detection (global + per-bar)
- Energy/loudness curve (per-bar RMS)
- Spectral centroid (per-bar, averaged over frames)
- Section boundaries using MFCC + SBic segmentation, snapped to bar edges (generic labels; semantic labeling deferred to mix planning)
- Vocal/instrumental detection per section (VGGish TF model)
- Output: separate `.features.json` file per track in `data/features/`

### 5. Mix Planning (Claude API)

AI creates a detailed mix plan based on the enriched metadata:

- Which sections of each track to use
- Transition points (where to crossfade, cut, or layer)
- Which track provides vocals vs. instrumentals at each point
- Single global target BPM (minimizes time-stretching artifacts)
- Pitch-shift requirements (integer semitones per track for key alignment)
- Per-segment effects as typed objects: high-pass, low-pass, reverb, delay, compressor — each with numeric parameters only, fully machine-parseable
- Crossfade durations in bars at segment boundaries
- Output: self-contained JSON mix plan with inlined source timestamps (no cross-referencing needed)

### 6. BPM Alignment & Pitch Shifting (pyrubberband)

Time-stretch and pitch-shift tracks according to the mix plan:

- Align both tracks to a common BPM (or BPM curve)
- Apply pitch corrections where key compatibility requires it
- Preserve audio quality (rubberband's high-quality time-stretching)

### 7. Mixdown (pedalboard)

Execute the mix plan using beat-grid reassembly:

- Cut prepared audio at detected beat positions, reassemble on a perfect tempo grid — eliminates drift
- Process each slice: layer tracks (vocals over instrumental) or play solo
- Apply EQ carving (high-pass on instrumental bed), effects, and gain per slice
- Short fade in/out at slice boundaries to prevent clicks
- Peak-normalize and export as FLAC + MP3

Uses pedalboard (Spotify) for effects processing, soundfile for FLAC I/O, lameenc for MP3 encoding.

## Logging

Every module logs to `logs/mashup.log` (append mode) with full timestamps. All pipeline steps, API calls, file I/O, and errors must be logged. DEBUG level captures detailed data (API responses, raw results); INFO level captures operational flow (what's happening, key metrics). Logs are essential for debugging long-running pipeline runs after the fact.

## Project Structure

```
src/
  prompts/
    track_selection.md        Prompt for Claude API: suggest two mashup-compatible tracks
    mix_planning.md           Prompt for Claude API: create mix plan from enriched metadata
```

## Tech Stack

- **Python 3.12+**
- **anthropic** — Claude API client for track selection and mix planning
- **beat_this** — beat/downbeat detection (CPJKU, ISMIR 2024)
- **essentia** — music information retrieval (key, sections, energy, spectral)
- **pyrubberband** — high-quality time-stretching and pitch-shifting (wraps rubberband CLI)
- **pedalboard** — audio effects, slicing, crossfading, mixdown, format conversion
- **yt-dlp** — YouTube audio downloading
- **soundfile** / **librosa** — audio I/O and utility functions

## Milestones

- [x] **M1 - Project Setup & Track Selection**: Python project scaffolding, Claude API integration for suggesting mashup-compatible track pairs
- [x] **M2 - Audio Acquisition & Beat Detection**: yt-dlp audio download, beat_this beat/downbeat detection, per-project directory structure, logging
- [x] **M3 - Feature Enrichment**: Essentia integration for key detection, section segmentation, energy curves, vocal detection
- [x] **M4 - Mix Planning**: Claude API integration that takes enriched metadata and outputs a structured JSON mix plan
- [x] **M5 - Time-Stretch & Pitch-Shift**: pyrubberband integration to align BPM and correct pitch per the mix plan
- [x] **M6 - Mixdown Engine**: Beat-grid mixdown via pedalboard, slice-based mix plan (layered + solo), AI provider abstraction (Anthropic + DeepSeek), web search for track selection, BPM cross-checking
- [ ] **M7 - End-to-End CLI**: Wire up the full pipeline as a CLI tool, add error handling, progress output, and configuration
