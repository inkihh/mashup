"""End-to-end pipeline runner with rich progress output."""

import logging
import os
import shutil
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from mashup.models import TrackSelection

logger = logging.getLogger("mashup.pipeline")

console = Console()


def _check_system_deps() -> None:
    """Check for required system dependencies."""
    missing = []
    if shutil.which("ffmpeg") is None:
        missing.append("ffmpeg")
    if shutil.which("rubberband") is None:
        missing.append("rubberband-cli (provides 'rubberband' command)")
    if missing:
        raise RuntimeError(
            f"Missing system dependencies: {', '.join(missing)}. "
            f"Install with: sudo apt install ffmpeg rubberband-cli"
        )


def _check_api_keys() -> None:
    """Check for required API keys based on provider."""
    provider = os.getenv("AI_PROVIDER", "anthropic").lower()
    if provider == "anthropic" and not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Add it to your .env file."
        )
    elif provider == "deepseek" and not os.getenv("DEEPSEEK_API_KEY"):
        raise RuntimeError(
            "DEEPSEEK_API_KEY not set. Add it to your .env file."
        )


def _step_header(step: int, total: int, name: str) -> str:
    return f"[{step}/{total}] {name}"


def run_pipeline(
    *,
    genre: str | None = None,
    mood: str | None = None,
    era: str | None = None,
    seed_artist: str | None = None,
    seed_title: str | None = None,
    output_dir: str = "output",
) -> Path:
    """Run the full mashup pipeline end-to-end.

    Returns the project directory path.
    """
    total_steps = 7
    completed: list[str] = []
    output_path = Path(output_dir)

    console.print()
    console.print(
        Panel(
            "[bold]Mashup Pipeline[/bold]\nGenerating an automatic music mashup",
            border_style="blue",
        )
    )
    console.print()

    try:
        # Pre-flight checks
        with console.status("[bold blue]Checking dependencies..."):
            _check_system_deps()
            _check_api_keys()
        console.print("[green]\u2713[/green] Dependencies and API keys OK")
        console.print()

        # Step 1: Track Selection
        project_dir = _step_select_tracks(
            step=1,
            total=total_steps,
            genre=genre,
            mood=mood,
            era=era,
            seed_artist=seed_artist,
            seed_title=seed_title,
            output_dir=output_path,
        )
        completed.append("Track selection")

        # Step 2: Download
        _step_download(2, total_steps, project_dir, output_path)
        completed.append("Download")

        # Step 3: Beat Detection
        _step_detect_beats(3, total_steps, project_dir)
        completed.append("Beat detection")

        # Step 4: Feature Enrichment
        _step_enrich(4, total_steps, project_dir)
        completed.append("Feature enrichment")

        # Step 5: Mix Planning
        _step_plan_mix(5, total_steps, project_dir)
        completed.append("Mix planning")

        # Step 6: Prepare Audio
        _step_prepare_audio(6, total_steps, project_dir)
        completed.append("Audio preparation")

        # Step 7: Mixdown
        output_files = _step_mixdown(7, total_steps, project_dir)
        completed.append("Mixdown")

        # Final summary
        console.print()
        console.print(
            Panel(
                "\n".join(
                    [
                        "[bold green]Mashup complete![/bold green]",
                        "",
                        f"Project: [cyan]{project_dir}[/cyan]",
                        "",
                        "Output files:",
                    ]
                    + [f"  [cyan]{p}[/cyan]" for p in output_files]
                ),
                border_style="green",
                title="Done",
            )
        )
        return project_dir

    except Exception as e:
        console.print()
        summary_parts = ["[bold red]Pipeline failed![/bold red]", ""]
        if completed:
            summary_parts.append(
                "Completed: " + ", ".join(f"[green]{s}[/green]" for s in completed)
            )
        summary_parts.append(f"Failed: [red]{e}[/red]")
        summary_parts.append("")
        summary_parts.append(
            "[dim]Check logs/mashup.log for details. "
            "Re-run to resume from the failed step.[/dim]"
        )
        console.print(Panel("\n".join(summary_parts), border_style="red", title="Error"))
        raise


def _print_step_start(step: int, total: int, name: str) -> None:
    console.print(
        f"[bold blue]{_step_header(step, total, name)}[/bold blue]"
    )


def _print_step_done(step: int, total: int, name: str, elapsed: float, detail: str = "") -> None:
    msg = f"[green]\u2713[/green] {name} [dim]({elapsed:.1f}s)[/dim]"
    if detail:
        msg += f"  {detail}"
    console.print(msg)
    console.print()


def _print_skip(step: int, total: int, name: str, reason: str) -> None:
    console.print(
        f"[yellow]\u2192[/yellow] {_step_header(step, total, name)} [dim]skipped ({reason})[/dim]"
    )
    console.print()


def _step_select_tracks(
    step: int,
    total: int,
    genre: str | None,
    mood: str | None,
    era: str | None,
    seed_artist: str | None,
    seed_title: str | None,
    output_dir: Path,
) -> Path:
    """Step 1: Select tracks. Returns the project directory."""
    name = "Selecting tracks"
    selection_path = output_dir / "track_selection.json"

    # Check if we already have a selection AND a project dir
    if selection_path.exists():
        selection = TrackSelection.model_validate_json(selection_path.read_text())
        from mashup.audio_download import project_dir_name

        proj_dir = output_dir / project_dir_name(
            selection.track_a.artist, selection.track_b.artist
        )
        proj_selection = proj_dir / "track_selection.json"
        if proj_selection.exists():
            _print_skip(step, total, name, "track_selection.json exists")
            return proj_dir

    _print_step_start(step, total, name)
    t0 = time.monotonic()

    from mashup.track_selection import select_tracks

    with console.status("[bold blue]  AI is selecting tracks..."):
        result = select_tracks(
            seed_artist=seed_artist,
            seed_title=seed_title,
            genre=genre,
            mood=mood,
            era=era,
        )

    result_json = result.model_dump_json(indent=2)
    output_dir.mkdir(parents=True, exist_ok=True)
    selection_path.write_text(result_json)

    detail = (
        f"[cyan]{result.track_a.artist} - {result.track_a.title}[/cyan]"
        f" x [cyan]{result.track_b.artist} - {result.track_b.title}[/cyan]"
    )
    _print_step_done(step, total, name, time.monotonic() - t0, detail)

    # Determine and return project dir path (download step will create it)
    from mashup.audio_download import project_dir_name

    return output_dir / project_dir_name(result.track_a.artist, result.track_b.artist)


def _step_download(step: int, total: int, project_dir: Path, output_dir: Path) -> None:
    """Step 2: Download audio."""
    name = "Downloading audio"
    input_dir = project_dir / "data" / "input"

    if input_dir.exists() and list(input_dir.glob("*.flac")):
        _print_skip(step, total, name, "audio files exist")
        return

    _print_step_start(step, total, name)
    t0 = time.monotonic()

    from mashup.audio_download import download_tracks_from_selection

    selection_path = output_dir / "track_selection.json"
    with console.status("[bold blue]  Downloading from YouTube..."):
        path_a, path_b = download_tracks_from_selection(selection_path, output_dir)

    _print_step_done(step, total, name, time.monotonic() - t0)


def _step_detect_beats(step: int, total: int, project_dir: Path) -> None:
    """Step 3: Beat detection."""
    name = "Detecting beats"
    beats_dir = project_dir / "data" / "beats"

    if beats_dir.exists() and list(beats_dir.glob("*.beats.json")):
        _print_skip(step, total, name, "beat files exist")
        return

    _print_step_start(step, total, name)
    t0 = time.monotonic()

    from mashup.beat_detection import detect_beats

    input_dir = project_dir / "data" / "input"
    flac_files = sorted(input_dir.glob("*.flac"))
    if not flac_files:
        raise FileNotFoundError(f"No FLAC files found in {input_dir}")

    beats_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for audio_path in flac_files:
        console.print(f"  [dim]Analyzing {audio_path.name}...[/dim]")
        with console.status(f"[bold blue]  Processing {audio_path.name}..."):
            result = detect_beats(audio_path)

        out_file = beats_dir / (audio_path.stem + ".beats.json")
        out_file.write_text(result.model_dump_json(indent=2))
        console.print(
            f"  [dim]{audio_path.name}: {result.bpm:.1f} BPM, "
            f"{len(result.beats)} beats, {result.time_signature}/4[/dim]"
        )
        results.append(result)

    # BPM compatibility check
    if len(results) == 2:
        bpm_a, bpm_b = results[0].bpm, results[1].bpm
        bpm_diff_pct = abs(bpm_a - bpm_b) / min(bpm_a, bpm_b) * 100
        if bpm_diff_pct > 15:
            raise RuntimeError(
                f"Detected BPMs are {bpm_diff_pct:.1f}% apart "
                f"({bpm_a:.1f} vs {bpm_b:.1f}). "
                f"These tracks may not work well together. "
                f"Consider re-running track selection."
            )

    _print_step_done(step, total, name, time.monotonic() - t0)


def _step_enrich(step: int, total: int, project_dir: Path) -> None:
    """Step 4: Feature enrichment."""
    name = "Enriching audio features"
    features_dir = project_dir / "data" / "features"

    if features_dir.exists() and list(features_dir.glob("*.features.json")):
        _print_skip(step, total, name, "feature files exist")
        return

    _print_step_start(step, total, name)
    t0 = time.monotonic()

    from mashup.feature_extraction import extract_features

    input_dir = project_dir / "data" / "input"
    beats_dir = project_dir / "data" / "beats"
    features_dir.mkdir(parents=True, exist_ok=True)

    flac_files = sorted(input_dir.glob("*.flac"))
    for audio_path in flac_files:
        beats_path = beats_dir / (audio_path.stem + ".beats.json")
        if not beats_path.exists():
            raise FileNotFoundError(
                f"No beats file for {audio_path.name}. "
                f"Run beat detection first."
            )

        console.print(f"  [dim]Enriching {audio_path.name}...[/dim]")
        with console.status(f"[bold blue]  Extracting features from {audio_path.name}..."):
            result = extract_features(audio_path, beats_path)

        out_file = features_dir / (audio_path.stem + ".features.json")
        out_file.write_text(result.model_dump_json(indent=2))
        vocal_count = sum(1 for s in result.sections if s.is_vocal)
        console.print(
            f"  [dim]{audio_path.name}: {result.global_key} {result.global_scale}, "
            f"{len(result.sections)} sections ({vocal_count} vocal)[/dim]"
        )

    _print_step_done(step, total, name, time.monotonic() - t0)


def _step_plan_mix(step: int, total: int, project_dir: Path) -> None:
    """Step 5: Mix planning."""
    name = "Planning mix"
    mix_plan_path = project_dir / "data" / "mix_plan.json"

    if mix_plan_path.exists():
        _print_skip(step, total, name, "mix_plan.json exists")
        return

    _print_step_start(step, total, name)
    t0 = time.monotonic()

    from mashup.mix_planning import plan_mix

    with console.status("[bold blue]  AI is creating the mix plan..."):
        result = plan_mix(project_dir)

    out_dir = project_dir / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    mix_plan_path.write_text(result.model_dump_json(indent=2))

    console.print(
        f"  [dim]Target BPM: {result.target_bpm}, "
        f"pitch A: {result.track_a_pitch_shift_semitones:+d}st, "
        f"pitch B: {result.track_b_pitch_shift_semitones:+d}st, "
        f"{len(result.slices)} slices[/dim]"
    )
    _print_step_done(step, total, name, time.monotonic() - t0)


def _step_prepare_audio(step: int, total: int, project_dir: Path) -> None:
    """Step 6: Time-stretch and pitch-shift."""
    name = "Preparing audio"
    prepared_dir = project_dir / "data" / "prepared"

    if prepared_dir.exists() and list(prepared_dir.glob("*.flac")):
        _print_skip(step, total, name, "prepared files exist")
        return

    _print_step_start(step, total, name)
    t0 = time.monotonic()

    from mashup.time_stretch import prepare_tracks

    with console.status("[bold blue]  Time-stretching and pitch-shifting..."):
        output_paths = prepare_tracks(project_dir)

    for p in output_paths:
        console.print(f"  [dim]Prepared: {p.name}[/dim]")

    _print_step_done(step, total, name, time.monotonic() - t0)


def _step_mixdown(step: int, total: int, project_dir: Path) -> list[Path]:
    """Step 7: Final mixdown. Returns output file paths."""
    name = "Mixing down"
    output_dir = project_dir / "data" / "output"

    if output_dir.exists() and list(output_dir.glob("*.flac")):
        _print_skip(step, total, name, "output files exist")
        return list(output_dir.glob("*.flac")) + list(output_dir.glob("*.mp3"))

    _print_step_start(step, total, name)
    t0 = time.monotonic()

    from mashup.mixdown import mixdown

    with console.status("[bold blue]  Rendering final mashup..."):
        output_paths = mixdown(project_dir)

    _print_step_done(step, total, name, time.monotonic() - t0)
    return output_paths
