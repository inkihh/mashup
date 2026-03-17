"""End-to-end pipeline runner with rich progress output."""

import logging
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from mashup.models import TrackSelection

logger = logging.getLogger("mashup.pipeline")

# Module-level console — replaced in quiet mode
console = Console()


@contextmanager
def _quiet_mode():
    """Redirect stdout/stderr at fd level to suppress library noise.

    Rich console keeps writing to the real terminal via a duped fd.
    """
    global console

    # Suppress TensorFlow C++ logging
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

    # Dup real stdout so Rich can still write to it
    console_fd = os.dup(1)
    console_file = os.fdopen(console_fd, "w")
    quiet_console = Console(file=console_file)

    # Save originals for restore
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)

    # Redirect both to /dev/null
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)

    old_console = console
    console = quiet_console
    try:
        yield
    finally:
        # Restore original fds
        os.dup2(saved_stdout, 1)
        os.dup2(saved_stderr, 2)
        os.close(saved_stdout)
        os.close(saved_stderr)
        console_file.close()
        console = old_console


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


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def _step_select_tracks(
    step: int,
    total: int,
    genre: str | None,
    mood: str | None,
    era: str | None,
    seed_artist: str | None,
    seed_title: str | None,
    track_b_artist: str | None,
    track_b_title: str | None,
    output_dir: Path,
) -> Path:
    """Step 1: Select tracks. Returns the project directory path."""
    name = "Selecting tracks"
    _print_step_start(step, total, name)
    t0 = time.monotonic()

    both_specified = seed_artist and seed_title and track_b_artist and track_b_title

    if both_specified:
        # Both tracks given — skip AI, create selection directly
        from mashup.models import Track

        result = TrackSelection(
            track_a=Track(artist=seed_artist, title=seed_title,
                          key="unknown", bpm=0, genre="unknown"),
            track_b=Track(artist=track_b_artist, title=track_b_title,
                          key="unknown", bpm=0, genre="unknown"),
            rationale="User-specified track pairing.",
        )
    else:
        from mashup.track_selection import select_tracks as _select

        with console.status("[bold blue]  AI is selecting tracks..."):
            result = _select(
                seed_artist=seed_artist,
                seed_title=seed_title,
                genre=genre,
                mood=mood,
                era=era,
            )

    result_json = result.model_dump_json(indent=2)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "track_selection.json").write_text(result_json)

    detail = (
        f"[cyan]{result.track_a.artist} - {result.track_a.title}[/cyan]"
        f" x [cyan]{result.track_b.artist} - {result.track_b.title}[/cyan]"
    )
    _print_step_done(step, total, name, time.monotonic() - t0, detail)

    from mashup.audio_download import project_dir_name

    return output_dir / project_dir_name(result.track_a.artist, result.track_b.artist)


def _step_download(step: int, total: int, project_dir: Path, output_dir: Path) -> None:
    """Step 2: Download audio."""
    name = "Downloading audio"
    _print_step_start(step, total, name)
    t0 = time.monotonic()

    from mashup.audio_download import download_tracks_from_selection

    selection_path = output_dir / "track_selection.json"
    with console.status("[bold blue]  Downloading from YouTube..."):
        download_tracks_from_selection(selection_path, output_dir)

    # Clean up temporary track_selection.json (now copied into project dir)
    if selection_path.exists() and (project_dir / "track_selection.json").exists():
        selection_path.unlink()

    _print_step_done(step, total, name, time.monotonic() - t0)


def _step_detect_beats(step: int, total: int, project_dir: Path, *, skip_existing: bool = False) -> None:
    """Step 3: Beat detection."""
    name = "Detecting beats"
    beats_dir = project_dir / "data" / "beats"

    if skip_existing and beats_dir.exists() and list(beats_dir.glob("*.beats.json")):
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


def _step_enrich(step: int, total: int, project_dir: Path, *, skip_existing: bool = False) -> None:
    """Step 4: Feature enrichment."""
    name = "Enriching audio features"
    features_dir = project_dir / "data" / "features"

    if skip_existing and features_dir.exists() and list(features_dir.glob("*.features.json")):
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


def _step_plan_mix(step: int, total: int, project_dir: Path, *, skip_existing: bool = False) -> None:
    """Step 5: Mix planning."""
    name = "Planning mix"
    mix_plan_path = project_dir / "data" / "mix_plan.json"

    if skip_existing and mix_plan_path.exists():
        _print_skip(step, total, name, "mix_plan.json exists")
        return

    _print_step_start(step, total, name)
    t0 = time.monotonic()

    from mashup.mix_planning import plan_mix

    with console.status("[bold blue]  AI is creating the mix plan (this can take a couple of minutes)..."):
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


def _step_prepare_audio(step: int, total: int, project_dir: Path, *, skip_existing: bool = False) -> None:
    """Step 6: Time-stretch and pitch-shift."""
    name = "Preparing audio"
    prepared_dir = project_dir / "data" / "prepared"

    if skip_existing and prepared_dir.exists() and list(prepared_dir.glob("*.flac")):
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


def _step_mixdown(step: int, total: int, project_dir: Path, *, skip_existing: bool = False) -> list[Path]:
    """Step 7: Final mixdown. Returns output file paths."""
    name = "Mixing down"
    output_dir = project_dir / "data" / "output"

    if skip_existing and output_dir.exists() and list(output_dir.glob("*.flac")):
        _print_skip(step, total, name, "output files exist")
        return list(output_dir.glob("*.flac")) + list(output_dir.glob("*.mp3"))

    _print_step_start(step, total, name)
    t0 = time.monotonic()

    from mashup.mixdown import mixdown

    with console.status("[bold blue]  Rendering final mashup..."):
        output_paths = mixdown(project_dir)

    _print_step_done(step, total, name, time.monotonic() - t0)
    return output_paths


# ---------------------------------------------------------------------------
# Pipeline orchestrators
# ---------------------------------------------------------------------------

def _run_steps_3_to_7(
    project_dir: Path,
    total_steps: int,
    completed: list[str],
    *,
    skip_existing: bool = False,
) -> list[Path]:
    """Run steps 3–7 (beat detection through mixdown)."""
    _step_detect_beats(3, total_steps, project_dir, skip_existing=skip_existing)
    completed.append("Beat detection")

    _step_enrich(4, total_steps, project_dir, skip_existing=skip_existing)
    completed.append("Feature enrichment")

    _step_plan_mix(5, total_steps, project_dir, skip_existing=skip_existing)
    completed.append("Mix planning")

    _step_prepare_audio(6, total_steps, project_dir, skip_existing=skip_existing)
    completed.append("Audio preparation")

    output_files = _step_mixdown(7, total_steps, project_dir, skip_existing=skip_existing)
    completed.append("Mixdown")
    return output_files


def _print_banner(subtitle: str = "Generating an automatic music mashup") -> None:
    console.print()
    console.print(
        Panel(
            f"[bold]Mashup Pipeline[/bold]\n{subtitle}",
            border_style="blue",
        )
    )
    console.print()


def _print_success(project_dir: Path, output_files: list[Path]) -> None:
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


def _print_failure(completed: list[str], error: Exception) -> None:
    console.print()
    summary_parts = ["[bold red]Pipeline failed![/bold red]", ""]
    if completed:
        summary_parts.append(
            "Completed: " + ", ".join(f"[green]{s}[/green]" for s in completed)
        )
    summary_parts.append(f"Failed: [red]{error}[/red]")
    summary_parts.append("")
    summary_parts.append(
        "[dim]Check logs/mashup.log for details. "
        "Use 'mashup resume' to retry from the failed step.[/dim]"
    )
    console.print(Panel("\n".join(summary_parts), border_style="red", title="Error"))


def run_pipeline(
    *,
    genre: str | None = None,
    mood: str | None = None,
    era: str | None = None,
    seed_artist: str | None = None,
    seed_title: str | None = None,
    track_b_artist: str | None = None,
    track_b_title: str | None = None,
    output_dir: str = "output",
    debug: bool = False,
) -> Path:
    """Run the full mashup pipeline from scratch.

    Returns the project directory path.
    """
    total_steps = 7
    completed: list[str] = []
    output_path = Path(output_dir)

    def _run() -> Path:
        _print_banner()

        try:
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
                track_b_artist=track_b_artist,
                track_b_title=track_b_title,
                output_dir=output_path,
            )
            completed.append("Track selection")

            # Step 2: Download
            _step_download(2, total_steps, project_dir, output_path)
            completed.append("Download")

            # Steps 3–7
            output_files = _run_steps_3_to_7(project_dir, total_steps, completed)

            _print_success(project_dir, output_files)
            return project_dir

        except Exception as e:
            _print_failure(completed, e)
            raise

    if debug:
        return _run()
    else:
        with _quiet_mode():
            return _run()


def resume_pipeline(
    project_dir: Path,
    *,
    debug: bool = False,
) -> Path:
    """Resume a pipeline from an existing project directory.

    Skips steps whose output files already exist.
    Returns the project directory path.
    """
    total_steps = 7
    completed: list[str] = []

    def _run() -> Path:
        _print_banner(subtitle=f"Resuming [cyan]{project_dir.name}[/cyan]")

        try:
            with console.status("[bold blue]Checking dependencies..."):
                _check_system_deps()
                _check_api_keys()
            console.print("[green]\u2713[/green] Dependencies and API keys OK")
            console.print()

            # Steps 1 & 2 are already done
            _print_skip(1, total_steps, "Selecting tracks", "project exists")
            completed.append("Track selection")
            _print_skip(2, total_steps, "Downloading audio", "project exists")
            completed.append("Download")

            # Steps 3–7 with skip logic
            output_files = _run_steps_3_to_7(
                project_dir, total_steps, completed, skip_existing=True
            )

            _print_success(project_dir, output_files)
            return project_dir

        except Exception as e:
            _print_failure(completed, e)
            raise

    if debug:
        return _run()
    else:
        with _quiet_mode():
            return _run()


def list_projects(output_dir: Path) -> list[Path]:
    """List project directories under the output directory."""
    if not output_dir.exists():
        return []
    projects = []
    for p in sorted(output_dir.iterdir()):
        if p.is_dir() and (p / "track_selection.json").exists():
            projects.append(p)
    return projects


def detect_project_status(project_dir: Path) -> str:
    """Return a short status string for a project directory."""
    data = project_dir / "data"
    if list((data / "output").glob("*.flac")) if (data / "output").exists() else []:
        return "[green]complete[/green]"
    if list((data / "prepared").glob("*.flac")) if (data / "prepared").exists() else []:
        return "[yellow]prepared \u2192 mixdown[/yellow]"
    if (data / "mix_plan.json").exists():
        return "[yellow]planned \u2192 prepare-audio[/yellow]"
    if list((data / "features").glob("*.features.json")) if (data / "features").exists() else []:
        return "[yellow]enriched \u2192 plan-mix[/yellow]"
    if list((data / "beats").glob("*.beats.json")) if (data / "beats").exists() else []:
        return "[yellow]beats \u2192 enrich[/yellow]"
    if list((data / "input").glob("*.flac")) if (data / "input").exists() else []:
        return "[yellow]downloaded \u2192 detect-beats[/yellow]"
    return "[red]empty[/red]"
