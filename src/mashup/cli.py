import logging
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

from mashup.log import setup_logging
from mashup.track_selection import select_tracks

logger = logging.getLogger("mashup.cli")

HELP_TEXT = """\
Automatic music mashup generator.

Run the full pipeline with 'mashup run', or resume an incomplete run with 'mashup resume'.
Individual steps can also be run separately for more control.

  \b
  run             Full pipeline — always starts fresh
  resume          Pick up an incomplete run from where it left off
  select-tracks   AI picks two compatible tracks
  download        Download audio from YouTube
  detect-beats    Beat and downbeat detection
  enrich          Extract audio features (key, energy, sections, vocals)
  plan-mix        AI creates a mix plan
  prepare-audio   Time-stretch and pitch-shift
  mixdown         Render the final mashup
"""


@click.group(help=HELP_TEXT)
def cli() -> None:
    load_dotenv()
    setup_logging()


def _parse_track(value: str) -> tuple[str, str]:
    """Parse 'Artist - Title' into (artist, title)."""
    if " - " not in value:
        raise click.UsageError(
            f"Invalid --track format: '{value}'. Expected 'Artist - Title'."
        )
    artist, title = value.split(" - ", 1)
    return artist.strip(), title.strip()


@cli.command("run")
@click.option("--genre", default=None, help="Preferred genre constraint.")
@click.option("--mood", default=None, help="Preferred mood (e.g., energetic, chill).")
@click.option("--era", default=None, help="Preferred era (e.g., 80s, 2010s).")
@click.option("--track", "tracks", multiple=True,
              help="Track as 'Artist - Title'. Use once to seed, twice to specify both.")
@click.option("--output-dir", default="output", help="Directory to save results.")
@click.option("--debug", is_flag=True, default=False, help="Show verbose output from all libraries.")
def run_cmd(
    genre: str | None,
    mood: str | None,
    era: str | None,
    tracks: tuple[str, ...],
    output_dir: str,
    debug: bool,
) -> None:
    """Run the full mashup pipeline end-to-end (always starts fresh).

    Chains all steps: track selection, download, beat detection, feature
    enrichment, mix planning, audio preparation, and mixdown.

    Use 'mashup resume' to pick up a failed or incomplete run.
    """
    if len(tracks) > 2:
        raise click.UsageError("At most two --track options allowed.")

    seed_artist = seed_title = track_b_artist = track_b_title = None
    if len(tracks) >= 1:
        seed_artist, seed_title = _parse_track(tracks[0])
    if len(tracks) == 2:
        track_b_artist, track_b_title = _parse_track(tracks[1])

    from mashup.pipeline import run_pipeline

    try:
        run_pipeline(
            genre=genre,
            mood=mood,
            era=era,
            seed_artist=seed_artist,
            seed_title=seed_title,
            track_b_artist=track_b_artist,
            track_b_title=track_b_title,
            output_dir=output_dir,
            debug=debug,
        )
    except Exception:
        sys.exit(1)


@cli.command("resume")
@click.option("--output-dir", default="output", type=click.Path(path_type=Path), help="Base output directory.")
@click.option("--debug", is_flag=True, default=False, help="Show verbose output from all libraries.")
def resume_cmd(output_dir: Path, debug: bool) -> None:
    """Resume an incomplete pipeline run.

    Shows a list of existing projects and lets you pick one to resume.
    Skips steps whose output files already exist.
    """
    from simple_term_menu import TerminalMenu

    from mashup.pipeline import (
        detect_project_status,
        list_projects,
        resume_pipeline,
    )

    projects = list_projects(output_dir)
    if not projects:
        click.echo(f"No projects found in {output_dir}/")
        sys.exit(1)

    # Build menu entries with status
    from rich.console import Console as _C
    from rich.text import Text

    plain_console = _C(highlight=False)

    entries = []
    for p in projects:
        status = detect_project_status(p)
        # Render rich markup to plain text for the menu
        text = Text.from_markup(f"{p.name}  {status}")
        entries.append(text.plain)

    menu = TerminalMenu(
        entries,
        title="Select a project to resume:",
    )
    idx = menu.show()

    if idx is None:
        # User cancelled
        sys.exit(0)

    selected = projects[idx]
    click.echo(f"Resuming: {selected}")
    click.echo()

    try:
        resume_pipeline(selected, debug=debug)
    except Exception:
        sys.exit(1)


@cli.command("select-tracks")
@click.option("--genre", default=None, help="Preferred genre constraint.")
@click.option("--mood", default=None, help="Preferred mood (e.g., energetic, chill).")
@click.option("--era", default=None, help="Preferred era (e.g., 80s, 2010s).")
@click.option("--track", "tracks", multiple=True,
              help="Track as 'Artist - Title'. Use once to seed, twice to specify both.")
@click.option("--output-dir", default="output", help="Directory to save results.")
def select_tracks_cmd(
    genre: str | None,
    mood: str | None,
    era: str | None,
    tracks: tuple[str, ...],
    output_dir: str,
) -> None:
    """Use AI to select two tracks for a mashup."""
    if len(tracks) > 2:
        raise click.UsageError("At most two --track options allowed.")

    seed_artist = seed_title = track_b_artist = track_b_title = None
    if len(tracks) >= 1:
        seed_artist, seed_title = _parse_track(tracks[0])
    if len(tracks) == 2:
        track_b_artist, track_b_title = _parse_track(tracks[1])

    logger.info("select-tracks: genre=%s mood=%s era=%s seed=%s", genre, mood, era, seed_artist)

    if seed_artist and seed_title and track_b_artist and track_b_title:
        # Both tracks specified — skip AI, create selection directly
        from mashup.models import Track, TrackSelection

        result = TrackSelection(
            track_a=Track(artist=seed_artist, title=seed_title, key="unknown", bpm=0, genre="unknown"),
            track_b=Track(artist=track_b_artist, title=track_b_title, key="unknown", bpm=0, genre="unknown"),
            rationale="User-specified track pairing.",
        )
    else:
        result = select_tracks(
            seed_artist=seed_artist,
            seed_title=seed_title,
            genre=genre,
            mood=mood,
            era=era,
        )

    result_json = result.model_dump_json(indent=2)
    click.echo(result_json)

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    (out_path / "track_selection.json").write_text(result_json)
    logger.info("Saved track selection to %s", out_path / "track_selection.json")
    click.echo(f"\nSaved to {out_path / 'track_selection.json'}")


@cli.command("download")
@click.option(
    "--selection",
    default="output/track_selection.json",
    type=click.Path(exists=True, path_type=Path),
    help="Path to track_selection.json.",
)
@click.option("--output-dir", default="output", type=click.Path(path_type=Path), help="Base output directory.")
def download_cmd(selection: Path, output_dir: Path) -> None:
    """Download audio for both tracks from YouTube."""
    from mashup.audio_download import download_tracks_from_selection

    logger.info("download: selection=%s output_dir=%s", selection, output_dir)
    path_a, path_b = download_tracks_from_selection(selection, output_dir)
    logger.info("Download complete: %s, %s", path_a, path_b)
    click.echo(f"\nTrack A: {path_a}")
    click.echo(f"Track B: {path_b}")


@cli.command("detect-beats")
@click.option(
    "--audio",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Path to a single audio file to analyze.",
)
@click.option(
    "--project-dir",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Path to a project directory (analyzes all FLAC files in it).",
)
def detect_beats_cmd(audio: Path | None, project_dir: Path | None) -> None:
    """Detect beats and downbeats in audio files."""
    from mashup.beat_detection import detect_beats

    if audio is None and project_dir is None:
        raise click.UsageError("Provide either --audio or --project-dir.")
    if audio is not None and project_dir is not None:
        raise click.UsageError("Provide either --audio or --project-dir, not both.")

    if audio is not None:
        files = [audio]
    else:
        input_dir = project_dir / "data" / "input"
        files = sorted(input_dir.glob("*.flac"))
        if not files:
            raise click.UsageError(f"No FLAC files found in {input_dir}")

    results = []
    for audio_path in files:
        logger.info("Analyzing: %s", audio_path)
        click.echo(f"\nAnalyzing: {audio_path.name}")
        result = detect_beats(audio_path)

        beats_dir = audio_path.parent.parent / "beats"
        beats_dir.mkdir(parents=True, exist_ok=True)
        out_file = beats_dir / (audio_path.stem + ".beats.json")
        out_file.write_text(result.model_dump_json(indent=2))

        logger.info("Beats detected: bpm=%.2f beats=%d downbeats=%d ts=%d/4",
                     result.bpm, len(result.beats), len(result.downbeats), result.time_signature)
        click.echo(f"  BPM: {result.bpm}")
        click.echo(f"  Beats: {len(result.beats)}")
        click.echo(f"  Downbeats (bars): {len(result.downbeats)}")
        click.echo(f"  Time signature: {result.time_signature}/4")
        click.echo(f"  Saved to: {out_file}")
        results.append(result)

    # BPM compatibility check (considers half/double-time)
    if len(results) == 2:
        from mashup.beat_utils import check_bpm_compatibility

        try:
            check_bpm_compatibility(results[0].bpm, results[1].bpm)
        except RuntimeError as e:
            click.echo(f"\n  WARNING: {e}")
            raise click.Abort()


@cli.command("enrich")
@click.option(
    "--project-dir",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to a project directory.",
)
def enrich_cmd(project_dir: Path) -> None:
    """Extract audio features (key, energy, sections, vocals) for all tracks."""
    from mashup.feature_extraction import extract_features

    input_dir = project_dir / "data" / "input"
    beats_dir = project_dir / "data" / "beats"
    features_dir = project_dir / "data" / "features"
    features_dir.mkdir(parents=True, exist_ok=True)

    flac_files = sorted(input_dir.glob("*.flac"))
    if not flac_files:
        raise click.UsageError(f"No FLAC files found in {input_dir}")

    for audio_path in flac_files:
        beats_path = beats_dir / (audio_path.stem + ".beats.json")
        if not beats_path.exists():
            raise click.UsageError(
                f"No beats file found for {audio_path.name}. "
                f"Run 'mashup detect-beats --project-dir {project_dir}' first."
            )

        logger.info("Enriching: %s", audio_path)
        click.echo(f"\nEnriching: {audio_path.name}")
        result = extract_features(audio_path, beats_path)

        out_file = features_dir / (audio_path.stem + ".features.json")
        out_file.write_text(result.model_dump_json(indent=2))

        click.echo(f"  Key: {result.global_key} {result.global_scale}")
        click.echo(f"  Bars: {len(result.bars)}")
        click.echo(f"  Sections: {len(result.sections)}")
        vocal_sections = sum(1 for s in result.sections if s.is_vocal)
        click.echo(f"  Vocal sections: {vocal_sections}/{len(result.sections)}")
        click.echo(f"  Saved to: {out_file}")


@cli.command("plan-mix")
@click.option(
    "--project-dir",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to a project directory.",
)
def plan_mix_cmd(project_dir: Path) -> None:
    """Create an AI-generated mix plan from enriched track features."""
    from mashup.mix_planning import plan_mix

    logger.info("plan-mix: project_dir=%s", project_dir)
    click.echo(f"Planning mix for {project_dir.name}...")

    result = plan_mix(project_dir)

    out_dir = project_dir / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "mix_plan.json"
    out_file.write_text(result.model_dump_json(indent=2))

    click.echo(f"\n  Target BPM: {result.target_bpm}")
    click.echo(f"  Pitch shift A: {result.track_a_pitch_shift_semitones:+d} semitones")
    click.echo(f"  Pitch shift B: {result.track_b_pitch_shift_semitones:+d} semitones")
    click.echo(f"  Slices: {len(result.slices)}")
    for i, s in enumerate(result.slices):
        parts = []
        if s.track_a:
            parts.append(f"A:{s.track_a.source_start:.1f}\u2013{s.track_a.source_end:.1f}s")
        if s.track_b:
            parts.append(f"B:{s.track_b.source_start:.1f}\u2013{s.track_b.source_end:.1f}s")
        mode = "layered" if s.track_a and s.track_b else "solo"
        click.echo(f"    {i+1:2d}. [{mode}] {' + '.join(parts)}")
    click.echo(f"\n  Saved to: {out_file}")


@cli.command("prepare-audio")
@click.option(
    "--project-dir",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to a project directory.",
)
def prepare_audio_cmd(project_dir: Path) -> None:
    """Time-stretch and pitch-shift tracks according to the mix plan."""
    from mashup.time_stretch import prepare_tracks

    logger.info("prepare-audio: project_dir=%s", project_dir)
    click.echo(f"Preparing audio for {project_dir.name}...")

    output_paths = prepare_tracks(project_dir)

    for path in output_paths:
        click.echo(f"  Prepared: {path}")
    click.echo(f"\n  Output directory: {output_paths[0].parent}")


@cli.command("mixdown")
@click.option(
    "--project-dir",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to a project directory.",
)
def mixdown_cmd(project_dir: Path) -> None:
    """Execute the mix plan and export the final mashup."""
    from mashup.mixdown import mixdown

    logger.info("mixdown: project_dir=%s", project_dir)
    click.echo(f"Mixing down {project_dir.name}...")

    output_paths = mixdown(project_dir)

    for path in output_paths:
        click.echo(f"  Output: {path}")
    click.echo(f"\n  Output directory: {output_paths[0].parent}")
