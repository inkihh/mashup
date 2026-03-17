# Mashup

Automatic music mashup generator. Combines two tracks from different genres into a cohesive mix, using AI for creative decisions and audio DSP libraries for processing.

See [CONCEPT.md](CONCEPT.md) for the full product vision and pipeline design, and [IMPLEMENTATION.md](IMPLEMENTATION.md) for the current technical state of the app.

## Status

Fully functional end-to-end pipeline: track selection, audio download, beat detection, audio feature enrichment, AI mix planning, audio preparation (time-stretch/pitch-shift), and mixdown with beat-grid alignment.

## Installation

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), [FFmpeg](https://ffmpeg.org/) (for audio conversion), and [Rubber Band](https://breakfastquay.com/rubberband/) (for time-stretching/pitch-shifting).

```bash
# Install system dependencies (if not already installed)
sudo apt install ffmpeg rubberband-cli    # Debian/Ubuntu
brew install ffmpeg rubberband            # macOS

# Set up Python environment
uv venv
source .venv/bin/activate
uv pip install -e .
```

Copy the example env file and configure:

```bash
cp .env-example .env
# edit .env — set your API key(s) and provider
```

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Anthropic API key (required for Anthropic provider) |
| `AI_PROVIDER` | `anthropic` | AI provider: `anthropic` or `deepseek` |
| `DEEPSEEK_API_KEY` | — | DeepSeek API key (required for DeepSeek provider) |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek API endpoint |
| `EFFECT_HIGH_PASS` | `true` | Enable high-pass filter effect |
| `EFFECT_LOW_PASS` | `true` | Enable low-pass filter effect |
| `EFFECT_REVERB` | `true` | Enable reverb effect |
| `EFFECT_DELAY` | `true` | Enable delay effect |
| `EFFECT_COMPRESSOR` | `true` | Enable compressor effect |

When using DeepSeek, track selection uses `deepseek-chat` and mix planning uses `deepseek-reasoner` (slower but more thoughtful plans).

## Usage

### Run the full pipeline

```bash
mashup run
```

Runs the entire mashup pipeline end-to-end: AI track selection, YouTube download, beat detection, feature enrichment, AI mix planning, time-stretch/pitch-shift, and final mixdown. Always starts fresh. Shows rich progress output with step indicators and timing.

Use `--debug` to see verbose output from all libraries (TensorFlow, yt-dlp, etc.).

#### Options

| Flag | Description |
|------|-------------|
| `--genre` | Preferred genre (e.g., `--genre "hip-hop"`) |
| `--mood` | Preferred mood (e.g., `--mood "energetic"`) |
| `--era` | Preferred era (e.g., `--era "80s"`) |
| `--seed-artist` + `--seed-title` | Provide one track, let AI pick the second |
| `--output-dir` | Output directory (default: `output/`) |
| `--debug` | Show verbose output from all libraries |

#### Examples

```bash
# Fully automatic
mashup run

# With constraints
mashup run --genre "electronic" --era "2010s"

# Seed one track
mashup run --seed-artist "Daft Punk" --seed-title "Around the World"
```

### Resume an incomplete run

```bash
mashup resume
```

Shows an interactive list of existing projects with their status (e.g., "enriched -> plan-mix"). Pick one with arrow keys to resume from where it left off — completed steps are skipped automatically.

| Flag | Description |
|------|-------------|
| `--output-dir` | Base output directory (default: `output/`) |
| `--debug` | Show verbose output from all libraries |

### Individual steps

Each pipeline step can also be run independently for more control.

### Select tracks for a mashup

```bash
mashup select-tracks
```

AI picks two tracks from different genres that work well together (compatible key, similar BPM, interesting genre contrast). Uses web search to look up accurate BPM/key data. The result is printed as JSON and saved to `output/track_selection.json`.

#### Options

| Flag | Description |
|------|-------------|
| `--genre` | Preferred genre (e.g., `--genre "hip-hop"`) |
| `--mood` | Preferred mood (e.g., `--mood "energetic"`) |
| `--era` | Preferred era (e.g., `--era "80s"`) |
| `--seed-artist` + `--seed-title` | Provide one track, let AI pick the second |
| `--output-dir` | Output directory (default: `output/`) |

#### Examples

```bash
# Fully AI-picked
mashup select-tracks

# With constraints
mashup select-tracks --genre "electronic" --era "2010s"

# Seed one track, let AI pick the match
mashup select-tracks --seed-artist "Daft Punk" --seed-title "Around the World"
```

### Download audio

```bash
mashup download
```

Downloads both tracks from YouTube as FLAC into a project subdirectory under `output/`. Requires a `track_selection.json` from the previous step.

| Flag | Description |
|------|-------------|
| `--selection` | Path to track_selection.json (default: `output/track_selection.json`) |
| `--output-dir` | Base output directory (default: `output/`) |

### Detect beats

```bash
mashup detect-beats --project-dir output/my_project/
```

Runs beat and downbeat detection using [beat_this](https://github.com/CPJKU/beat_this) (ISMIR 2024), cross-checked with [Essentia](https://essentia.upf.edu/) for BPM reliability. Outputs a `.beats.json` file per track. Aborts if detected BPMs are >15% apart (incompatible tracks).

| Flag | Description |
|------|-------------|
| `--audio` | Path to a single audio file |
| `--project-dir` | Path to a project directory (analyzes all FLAC files in it) |

### Enrich audio features

```bash
mashup enrich --project-dir output/my_project/
```

Extracts musical features from audio using [Essentia](https://essentia.upf.edu/). Requires beat detection to have been run first. Outputs a `.features.json` file per track with:

- Global and per-bar key/scale detection
- Per-bar RMS energy and spectral centroid
- Structural section boundaries (snapped to bar edges)
- Per-section vocal/instrumental classification (VGGish TF model, auto-downloaded on first use)

| Flag | Description |
|------|-------------|
| `--project-dir` | Path to a project directory (required) |

### Plan mix

```bash
mashup plan-mix --project-dir output/my_project/
```

Uses AI to create a mix plan from the enriched track features. The plan is a sequence of slices — each slice layers vocals from one track over the instrumental of the other, or plays one track solo. Outputs `mix_plan.json` with:

- Target BPM and per-track pitch shift (semitones)
- Ordered slices with source timestamps, gain, and effects per track
- Self-contained — the mixdown step needs no cross-referencing

| Flag | Description |
|------|-------------|
| `--project-dir` | Path to a project directory (required) |

### Prepare audio

```bash
mashup prepare-audio --project-dir output/my_project/
```

Time-stretches and pitch-shifts both tracks according to the mix plan. Outputs prepared FLAC files to `data/prepared/`:

- Time-stretches each track to the mix plan's target BPM using [Rubber Band](https://breakfastquay.com/rubberband/)
- Applies formant-preserving pitch shift (in semitones) for key alignment
- Copies files as-is when no processing is needed

| Flag | Description |
|------|-------------|
| `--project-dir` | Path to a project directory (required) |

### Mixdown

```bash
mashup mixdown --project-dir output/my_project/
```

Executes the mix plan and exports the final mashup. Uses beat-grid reassembly to keep tracks locked to tempo. Outputs to `data/output/`:

- Cuts each track at detected beat positions and reassembles on a perfect tempo grid
- Layers tracks within slices (vocals over instrumental with EQ carving)
- Applies per-slice gain and effects (high-pass, low-pass, reverb, delay, compressor) via [Pedalboard](https://github.com/spotify/pedalboard)
- Short fade in/out on slice boundaries to prevent clicks
- Peak-normalizes to -1 dBFS
- Exports as FLAC (lossless) and MP3 (192 kbps)

| Flag | Description |
|------|-------------|
| `--project-dir` | Path to a project directory (required) |
