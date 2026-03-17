import logging
import re
import shutil
from pathlib import Path

import click
import yt_dlp

from mashup.models import TrackSelection

logger = logging.getLogger("mashup.audio_download")


def sanitize_name(name: str) -> str:
    """Lowercase, replace non-alphanumeric with underscores, collapse multiples."""
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    return name.strip("_")


def project_dir_name(artist_a: str, artist_b: str) -> str:
    return f"{sanitize_name(artist_a)}_x_{sanitize_name(artist_b)}"


def download_track(artist: str, title: str, output_dir: Path) -> Path:
    """Download a track from YouTube as FLAC. Returns path to the audio file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{sanitize_name(artist)}_{sanitize_name(title)}"
    output_template = str(output_dir / filename)

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template + ".%(ext)s",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "flac",
            }
        ],
        "default_search": "ytsearch",
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
    }

    query = f"{artist} - {title}"
    logger.info("Searching YouTube for: %s", query)
    click.echo(f"Searching YouTube for: {query}")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([query])

    result_path = output_dir / f"{filename}.flac"
    if not result_path.exists():
        logger.error("Download failed — expected file not found: %s", result_path)
        raise FileNotFoundError(f"Download failed — expected file not found: {result_path}")

    logger.info("Downloaded: %s (%.1f MB)", result_path, result_path.stat().st_size / 1e6)
    click.echo(f"Downloaded: {result_path}")
    return result_path


def download_tracks_from_selection(
    selection_path: Path,
    output_dir: Path,
) -> tuple[Path, Path]:
    """Read track_selection.json, create project dir, download both tracks."""
    logger.info("Loading track selection from %s", selection_path)
    raw = selection_path.read_text()
    selection = TrackSelection.model_validate_json(raw)

    proj_dir = output_dir / project_dir_name(
        selection.track_a.artist, selection.track_b.artist
    )
    proj_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Project directory: %s", proj_dir)

    # Copy track_selection.json into the project directory
    dest_selection = proj_dir / "track_selection.json"
    if selection_path.resolve() != dest_selection.resolve():
        shutil.copy2(selection_path, dest_selection)

    input_dir = proj_dir / "data" / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    path_a = download_track(
        selection.track_a.artist, selection.track_a.title, input_dir
    )
    path_b = download_track(
        selection.track_b.artist, selection.track_b.title, input_dir
    )

    return path_a, path_b


def track_filenames(selection: TrackSelection) -> tuple[str, str]:
    """Return the expected FLAC filenames for track_a and track_b (without path)."""
    name_a = f"{sanitize_name(selection.track_a.artist)}_{sanitize_name(selection.track_a.title)}.flac"
    name_b = f"{sanitize_name(selection.track_b.artist)}_{sanitize_name(selection.track_b.title)}.flac"
    return name_a, name_b
