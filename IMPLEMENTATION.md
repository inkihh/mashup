# Mashup — Implementation

## Project Structure

```
pyproject.toml                          Project config, dependencies, CLI entry point
.env-example                            Template for API keys and AI provider config
.gitignore                              Excludes .env, output/, logs/, .venv/, model files, etc.
src/mashup/
  __init__.py
  ai.py                                 AI provider abstraction (Anthropic, DeepSeek)
  cli.py                                Click CLI — entry point, subcommands
  log.py                                Logging setup (append to logs/mashup.log)
  models.py                             Pydantic models (Track, TrackSelection, TrackBeats, TrackFeatures, MixPlan, effects)
  track_selection.py                    Builds prompt, calls AI with web search, validates response
  audio_download.py                     Downloads tracks from YouTube via yt-dlp as FLAC
  beat_detection.py                     Beat/downbeat detection via beat_this, cross-checked with essentia
  feature_extraction.py                 Audio feature extraction via Essentia (key, energy, sections, vocals)
  mix_planning.py                       Builds mix planning prompt, calls AI, validates MixPlan
  time_stretch.py                       Time-stretch and pitch-shift via pyrubberband
  mixdown.py                            Beat-grid mixdown engine via pedalboard
  models/                               Auto-downloaded TF model weights (gitignored)
  prompts/
    __init__.py
    track_selection.md                  Jinja2 prompt template for track selection
    mix_planning.md                     Jinja2 prompt template for mix planning
```

## Per-Project Output Layout

Each mashup run creates a project directory named `{artist_a}_x_{artist_b}` under `output/`:

```
output/
  track_selection.json                  Temporary landing spot from select-tracks
  {artist_a}_x_{artist_b}/
    track_selection.json                Copied in by download command
    data/
      input/                            Downloaded audio (FLAC)
      beats/                            Beat/downbeat metadata (JSON)
      features/                         Enriched audio features (JSON)
      mix_plan.json                     AI-generated mix plan (JSON; timestamps updated after prepare-audio)
      prepared/                         Time-stretched and pitch-shifted audio (FLAC)
      output/                           Final mashup output (FLAC + MP3)
```

## Architecture

### AI Provider (`ai.py`)
- Abstracts AI API calls behind a `chat()` function
- Supports **Anthropic** (Claude) and **DeepSeek** (OpenAI-compatible API)
- Provider configurable via `AI_PROVIDER` env var (`anthropic` or `deepseek`)
- Default models per provider and task type (select vs plan)
- Anthropic: supports web search tool for track selection (grounded BPM/key lookup)
- DeepSeek: uses `openai` SDK with custom `DEEPSEEK_BASE_URL`

### CLI (`cli.py`)
- Uses **Click** for subcommands and options
- Loads `.env` via **python-dotenv** and initializes logging on startup
- Subcommands: `select-tracks`, `download`, `detect-beats`, `enrich`, `plan-mix`, `prepare-audio`, `mixdown`
- Post-beat-detection BPM compatibility check: aborts if detected BPMs are >15% apart

### Logging (`log.py`)
- Configures the `mashup` logger on first call to `setup_logging()`
- Appends to `logs/mashup.log` (DEBUG level) with timestamps
- Also logs to stderr (INFO level)
- Every run starts with a `---------- NEW RUN ---------------------` marker
- All modules use `logging.getLogger("mashup.<module>")` for consistent namespacing

### Track Selection (`track_selection.py`)
- Loads the Jinja2 prompt template from `prompts/track_selection.md`
- Injects optional user constraints (genre, mood, era) and seed track info
- Calls AI via `ai.chat()` with `web_search=True` — the model searches the web for accurate BPM/key data rather than guessing from memory
- Parses and validates the JSON response through a **Pydantic** model (`TrackSelection`)
- Robust JSON extraction handles extra text around the JSON object

### Audio Download (`audio_download.py`)
- `download_track()` searches YouTube via yt-dlp and extracts audio as FLAC
- `download_tracks_from_selection()` reads `track_selection.json`, creates the project directory structure, and downloads both tracks to `data/input/`
- `track_filenames()` helper derives expected FLAC filenames from track selection — used by all downstream steps to correctly match files to track A/B (avoids alphabetical sorting bugs)
- Sanitizes artist/title names for filesystem-safe filenames

### Beat Detection (`beat_detection.py`)
- Uses **beat_this** (`File2Beats`) for neural beat and downbeat detection
- Cross-checks BPM with **essentia** `RhythmExtractor2013` for reliability
- When detectors agree (within 5%): averages them
- When one is double the other: prefers the slower "groove BPM" to avoid double-time false positives
- When they disagree entirely: averages as best guess
- Infers time signature by counting beats between consecutive downbeats
- Outputs `TrackBeats` model saved as JSON to `data/beats/`

### Feature Extraction (`feature_extraction.py`)
- Uses **essentia-tensorflow** for audio feature extraction
- Takes audio files + matching `.beats.json` as input; uses downbeats as the bar-slicing grid
- **Per-bar features**: key/scale (via `KeyExtractor`), RMS energy, spectral centroid (averaged over frames within each bar)
- **Global features**: key/scale and RMS energy for the full track
- **Section segmentation**: MFCC extraction → SBic (Bayesian Information Criterion) boundary detection → boundaries snapped to nearest bar edge; sections get generic labels (`section_1`, `section_2`, …)
- **Vocal/instrumental detection**: per-section classification using VGGish embeddings + voice/instrumental TF model head; model files auto-downloaded from essentia.upf.edu on first use to `src/mashup/models/`
- Outputs `TrackFeatures` model saved as JSON to `data/features/`

### Models (`models.py`)
- `Track`: artist, title, key, bpm, genre
- `TrackSelection`: track_a, track_b, rationale
- `TrackBeats`: audio_file, bpm, beats (timestamps), downbeats (timestamps), time_signature
- `BarFeatures`: start, end, key, scale, energy, spectral_centroid
- `Section`: start, end, label, is_vocal, mean_energy, mean_spectral_centroid
- `TrackFeatures`: audio_file, bpm, time_signature, global_key, global_scale, global_energy, bars (list of BarFeatures), sections (list of Section)
- `HighPass`, `LowPass`, `Reverb`, `Delay`, `Compressor`: typed effect models with numeric parameters, discriminated union via `type` field → `TrackEffect`
- `MixTrackRole`: source_start, source_end, gain_db, effects (list of TrackEffect)
- `MixSlice`: track_a (optional MixTrackRole), track_b (optional MixTrackRole) — supports both layered and solo slices
- `MixPlan`: target_bpm, track_a_pitch_shift_semitones, track_b_pitch_shift_semitones, slices (list of MixSlice), rationale

### Mix Planning (`mix_planning.py`)
- Loads `track_selection.json` and both `.features.json` files from the project directory
- Uses `track_filenames()` to correctly match feature files to track A/B
- `build_mix_prompt()` renders the Jinja2 template with track metadata and section data
- Calls AI via `ai.chat()` with `task="plan"`
- Parses and validates JSON response into `MixPlan` Pydantic model
- Saves result as `data/mix_plan.json`

### Time Stretch (`time_stretch.py`)
- `prepare_track()` loads audio via **soundfile**, applies `pyrb.time_stretch()` then `pyrb.pitch_shift()` (two-pass via **pyrubberband**), saves as FLAC to `data/prepared/`
- Pitch shifting uses `--formant` flag to preserve vocal formants
- Copies files as-is when no processing is needed (BPM already matches and pitch shift is 0)
- `prepare_tracks()` orchestrates both tracks: uses `track_filenames()` for correct A/B assignment, reads `mix_plan.json` for target BPM and pitch shifts, reads `.beats.json` for original BPMs
- After processing, updates `mix_plan.json` in place — scales all `source_start`/`source_end` timestamps by `original_bpm / target_bpm` per track so the mix plan references the prepared audio directly
- Requires system dependency: `rubberband-cli` (Debian/Ubuntu) or `rubberband` (macOS)

### Mixdown (`mixdown.py`)
- **Beat-grid reassembly**: cuts prepared audio at detected beat positions and places each beat slice on a perfect tempo grid (`60/target_bpm` spacing), eliminating drift even with tiny BPM differences
- **Slice-based architecture**: each slice can layer both tracks (vocals over instrumental) or play one solo — the AI decides the arrangement
- **Effects**: maps model effect types to pedalboard plugins (HighpassFilter, LowpassFilter, Reverb, Delay, Compressor)
- **Anti-click fades**: 256-sample (~5ms) fade-in/fade-out on every slice boundary; 128-sample crossfade at beat splice points within slices
- **Peak normalization**: normalizes final output to -1 dBFS
- **Export**: FLAC (lossless via soundfile) and MP3 (192 kbps via pedalboard + lameenc) to `data/output/`

### Prompts (`prompts/`)
- `track_selection.md` — Jinja2 template with conditional sections for seed track and user constraints; selection criteria: same key, near-identical BPM (within 5%), same rhythmic feel, genre contrast, mega-hits only; instructs the model to use web search for accurate BPM/key lookup; blacklist of overused tracks
- `mix_planning.md` — Jinja2 template receiving both tracks' features and selection metadata; instructs the AI on mashup production principles (layer vocals over instrumentals, EQ carving with high-pass on instrumental bed, energy arc, hard cuts at phrase boundaries); defines slice-based JSON schema with typed effect objects

## Dependencies

- `anthropic` — Claude API client
- `openai` — OpenAI-compatible API client (used for DeepSeek)
- `click` — CLI framework
- `pydantic` — data validation and JSON parsing
- `jinja2` — prompt templating
- `python-dotenv` — .env file loading
- `yt-dlp` — YouTube audio downloading
- `beat_this` — beat/downbeat detection (CPJKU, ISMIR 2024; installed from GitHub)
- `torch` — PyTorch (beat_this dependency)
- `soundfile` — audio I/O backend for beat_this
- `essentia-tensorflow` — audio feature extraction (key, energy, spectral, vocal detection via TF models)
- `pyrubberband` — Python wrapper for Rubber Band (time-stretching and pitch-shifting)
- `pedalboard` — audio effects processing (Spotify)
- `lameenc` — MP3 encoding support for pedalboard
- `numpy` — numerical operations

## Changelog

### 0.1.0 (unpublished)
- Project scaffolding with uv/hatchling build system
- Track selection via Claude API with genre/mood/era constraints and seed track support
- CLI entry point (`mashup select-tracks`)
- Output to stdout and `output/track_selection.json`

### 0.2.0 (unpublished)
- Audio download from YouTube via yt-dlp as FLAC (`mashup download`)
- Beat/downbeat detection via beat_this with BPM and time signature inference (`mashup detect-beats`)
- Per-project directory structure (`output/{artist_a}_x_{artist_b}/data/{input,beats,output}`)
- Logging to `logs/mashup.log` with timestamps across all modules

### 0.3.0 (unpublished)
- Audio feature extraction via Essentia (`mashup enrich --project-dir`)
- Per-bar key/scale detection, RMS energy, and spectral centroid using downbeats as analysis grid
- Structural section segmentation via MFCC + SBic, snapped to bar boundaries
- Per-section vocal/instrumental classification using VGGish TF model (auto-downloads weights)
- Separate `.features.json` output files in `data/features/`

### 0.4.0 (unpublished)
- AI-generated mix planning via Claude API (`mashup plan-mix --project-dir`)
- Self-contained mix plan JSON with inlined source timestamps per segment
- Section-level granularity: Claude selects from Essentia-detected sections
- Single global target BPM, per-track pitch shift in semitones (decided by Claude)
- Typed effect system: high_pass, low_pass, reverb, delay, compressor — all with numeric parameters
- Crossfade durations in bars at segment boundaries
- Pydantic discriminated union for effect types ensures strict validation

### 0.5.0 (unpublished)
- Time-stretch and pitch-shift via pyrubberband (`mashup prepare-audio --project-dir`)
- Two-pass processing: time-stretch to target BPM, then pitch-shift by semitones
- Copies files as-is when no processing is needed
- Automatic mix plan timestamp recalculation after time-stretching
- Prepared audio output to `data/prepared/`

### 0.6.0 (unpublished)
- Mixdown engine via pedalboard (`mashup mixdown --project-dir`)
- Beat-grid reassembly: cuts at detected beat positions, places on perfect tempo grid — eliminates drift
- Slice-based mix plan: each slice can layer both tracks or play one solo
- AI plans mashups as vocals-over-instrumental layers with EQ carving (high-pass on instrumental bed)
- Effects pipeline: high-pass, low-pass, reverb, delay, compressor mapped to pedalboard plugins
- Per-slice fade in/out (5ms) and per-beat-splice crossfade (128 samples) to prevent clicks
- Peak normalization to -1 dBFS
- Export as FLAC + MP3 (192 kbps)
- AI provider abstraction (`ai.py`): supports Anthropic (Claude) and DeepSeek, configurable via `AI_PROVIDER` env var
- Web search enabled for track selection — model looks up real BPM/key data instead of guessing
- BPM detection cross-check: beat_this + essentia consensus, half/double-time correction (prefer groove BPM)
- Post-beat-detection BPM compatibility gate (>15% aborts)
- Correct track A/B file assignment via `track_filenames()` throughout pipeline
- Formant-preserving pitch shifting via rubberband `--formant` flag
- Configurable effects: per-effect-type toggles via `EFFECT_*` env vars (high_pass, low_pass, reverb, delay, compressor)
- AI provider reads env vars lazily (after dotenv loads), not at import time
- Track selection uses Anthropic with web search when available; falls back to DeepSeek without web search
- DeepSeek Reasoner support (`deepseek-reasoner`) for mix planning with increased token limit
- Effect field name normalization: handles AI model variants (e.g. `frequency` → `freq_hz`)
