"""Typer commands: auth, poll, generate, runs."""

from collections.abc import Callable, Sequence

import typer
import typer.rich_utils as rich_utils
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from music_discovery.db import session_scope
from music_discovery.jobs.poll_history import run_poll
from music_discovery.jobs.weekly import run_weekly
from music_discovery.models import JobRun, RecommendationRun
from music_discovery.pipeline.publish import publish_playlist
from music_discovery.spotify.auth import AuthError, run_oauth

rich_utils.STYLE_COMMANDS_PANEL_BORDER = "cyan"
rich_utils.STYLE_OPTIONS_PANEL_BORDER = "cyan"
rich_utils.STYLE_USAGE = "bold cyan"

_STATUS_STYLES = {
    "succeeded": "green",
    "published": "green",
    "failed": "red",
    "running": "yellow",
    "pending": "yellow",
    "dry_run": "cyan",
}

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help=(
        "Weekly private Spotify playlists of tracks you have [bold]never[/bold] played.\n\n"
        "[dim]A heard song stays out, matched on Spotify track id and on "
        "normalized artist + title.[/dim]"
    ),
)


def _stdout() -> Console:
    # Built per call so tests that redirect stdout still receive the render.
    return Console()


def _stderr() -> Console:
    return Console(stderr=True)


def _panel(message: str, *, title: str, style: str) -> Panel:
    return Panel(
        escape(message),
        title=title,
        title_align="left",
        border_style=style,
        box=box.ROUNDED,
        padding=(1, 2),
    )


def _call(fn: Callable[[], None]) -> None:
    try:
        fn()
    except (NotImplementedError, AuthError) as exc:
        _stderr().print(_panel(str(exc), title="Not ready", style="yellow"))
        raise typer.Exit(code=1) from exc


def _status(status: str) -> str:
    style = _STATUS_STYLES.get(status, "white")
    return f"[{style}]{escape(status)}[/{style}]"


def _jobs_table(rows: Sequence[JobRun]) -> Table:
    table = Table(
        title="Jobs",
        title_style="bold",
        box=box.ROUNDED,
        border_style="cyan",
        header_style="bold cyan",
        expand=True,
    )
    table.add_column("ID", justify="right", style="dim", no_wrap=True)
    table.add_column("Job", style="bold", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Started", no_wrap=True)
    table.add_column("Notes")
    for row in rows:
        notes: list[str] = []
        if row.gap_warning:
            notes.append("[yellow]gap[/yellow]")
        if row.error:
            notes.append(f"[red]{escape(row.error)}[/red]")
        table.add_row(
            str(row.id),
            escape(row.job_name),
            _status(row.status),
            escape(row.started_at.isoformat(timespec="seconds")),
            "  ".join(notes) if notes else "[dim]—[/dim]",
        )
    return table


def _recommendations_table(rows: Sequence[RecommendationRun]) -> Table:
    table = Table(
        title="Recommendations",
        title_style="bold",
        box=box.ROUNDED,
        border_style="cyan",
        header_style="bold cyan",
        expand=True,
    )
    table.add_column("ID", justify="right", style="dim", no_wrap=True)
    table.add_column("Week", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Playlist")
    for row in rows:
        playlist = (
            escape(row.spotify_playlist_id) if row.spotify_playlist_id else "[dim]—[/dim]"
        )
        table.add_row(
            str(row.id),
            escape(str(row.week_start)),
            _status(row.status),
            playlist,
        )
    return table


def _empty(console: Console, title: str, message: str) -> None:
    console.print(
        Panel(
            f"[dim]{message}[/dim]",
            title=title,
            title_align="left",
            border_style="cyan",
            box=box.ROUNDED,
            padding=(1, 2),
        )
    )


@app.command()
def auth() -> None:
    """One-time Spotify sign-in. Run on the host; stores an encrypted refresh token."""
    _call(run_oauth)


@app.command()
def poll() -> None:
    """Poll recently-played and refresh saved tracks."""
    _call(run_poll)


@app.command()
def generate(
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--publish",
        help="Score a playlist without creating it. Publishing is not wired yet.",
    ),
) -> None:
    """Build this week's playlist."""
    if dry_run:
        _call(run_weekly)
        return
    _call(publish_playlist)


@app.command()
def runs() -> None:
    """Show job and recommendation history."""
    try:
        with session_scope() as session:
            job_rows = session.scalars(
                select(JobRun).order_by(JobRun.started_at.desc()).limit(20)
            ).all()
            recommendation_rows = session.scalars(
                select(RecommendationRun).order_by(RecommendationRun.week_start.desc()).limit(20)
            ).all()
    except OperationalError as exc:
        detail = str(exc.orig) if exc.orig is not None else str(exc)
        _stderr().print(_panel(detail, title="Database unavailable", style="red"))
        raise typer.Exit(code=1) from exc

    console = _stdout()
    if job_rows:
        console.print(_jobs_table(job_rows))
    else:
        _empty(console, "Jobs", "No job runs yet.")
    console.print()
    if recommendation_rows:
        console.print(_recommendations_table(recommendation_rows))
    else:
        _empty(console, "Recommendations", "No recommendation runs yet.")
