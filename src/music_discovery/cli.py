"""Typer commands: auth, profile, poll, played, generate, runs."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import typer
import typer.rich_utils as rich_utils
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from music_discovery.config import get_settings
from music_discovery.db import session_scope
from music_discovery.jobs.poll_history import PollError, run_poll
from music_discovery.jobs.weekly import run_weekly
from music_discovery.models import JobRun, PlayEvent, RecommendationRun, Track
from music_discovery.pipeline.publish import publish_playlist
from music_discovery.spotify.auth import AuthError, ensure_access_token, run_oauth
from music_discovery.spotify.client import (
    SpotifyAuthError,
    SpotifyClient,
    SpotifyClientError,
    SpotifyUser,
)

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


def _stdout(*, highlight: bool = True) -> Console:
    # Built per call so tests that redirect stdout still receive the render.
    return Console(highlight=highlight)


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
    except (NotImplementedError, AuthError, PollError) as exc:
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


@dataclass(frozen=True)
class PlayedTrack:
    primary_artist: str
    name: str
    played_at: datetime


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _as_local(value: datetime) -> datetime:
    return _as_utc(value).astimezone()


def _format_absolute(played_at: datetime) -> str:
    local = _as_local(played_at)
    hour = int(local.strftime("%I"))
    clock = f"{hour}:{local.strftime('%M %p')}"
    return f"{local.strftime('%a')}, {local.strftime('%b')} {local.day}, {local.year}  {clock}"


def _relative_phrase(played_at: datetime, *, now: datetime) -> str | None:
    seconds = int((now - _as_utc(played_at)).total_seconds())
    if seconds < 0:
        return None
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 7:
        return f"{days}d ago"
    weeks = days // 7
    if weeks < 5:
        return f"{weeks}w ago"
    return None


def _played_cell(played_at: datetime, *, now: datetime) -> str:
    absolute = escape(_format_absolute(played_at))
    relative = _relative_phrase(played_at, now=now)
    if relative is None:
        return absolute
    return f"{absolute}   [dim]{escape(relative)}[/dim]"


def _load_played_tracks(session: Session) -> list[PlayedTrack]:
    statement = (
        select(Track.primary_artist, Track.name, PlayEvent.played_at)
        .select_from(PlayEvent)
        .join(Track, Track.spotify_track_id == PlayEvent.spotify_track_id)
        .order_by(PlayEvent.played_at.desc())
    )
    return [
        PlayedTrack(artist, name, played_at)
        for artist, name, played_at in session.execute(statement)
    ]


def _played_table(rows: Sequence[PlayedTrack], *, now: datetime) -> Table:
    count = len(rows)
    noun = "play" if count == 1 else "plays"
    table = Table(
        title="Recently played",
        title_style="bold",
        title_justify="left",
        caption=f"{count} {noun}  ·  newest first",
        caption_style="dim",
        caption_justify="left",
        box=box.ROUNDED,
        border_style="cyan",
        header_style="bold cyan",
        expand=True,
        padding=(0, 1),
    )
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Artist", style="bold", ratio=2, overflow="fold")
    table.add_column("Track", ratio=3, overflow="fold")
    table.add_column("Played", no_wrap=True)
    for index, row in enumerate(rows, start=1):
        day = _as_local(row.played_at).date()
        next_day = _as_local(rows[index].played_at).date() if index < count else day
        table.add_row(
            str(index),
            escape(row.primary_artist),
            escape(row.name),
            _played_cell(row.played_at, now=now),
            end_section=next_day != day,
        )
    return table


_PRODUCT_LABELS = {
    "premium": "Premium",
    "free": "Free",
    "open": "Open",
}


def load_profile(*, http_client: httpx.Client | None = None) -> SpotifyUser:
    """Read the signed-in account from ``GET /me``."""
    settings = get_settings()
    owns_client = http_client is None
    http = http_client or httpx.Client(timeout=30)
    try:
        holder = {
            "token": ensure_access_token(settings=settings, http_client=http),
        }

        def token() -> str:
            return holder["token"]

        def refresh() -> str:
            holder["token"] = ensure_access_token(
                settings=settings,
                http_client=http,
                force=True,
            )
            return holder["token"]

        return SpotifyClient(token, http_client=http, refresh=refresh).get_me()
    finally:
        if owns_client:
            http.close()


def _profile_table(user: SpotifyUser) -> Table:
    table = Table(
        title="Spotify profile",
        title_style="bold",
        box=box.ROUNDED,
        border_style="cyan",
        header_style="bold cyan",
        show_header=False,
        expand=True,
    )
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    for label, value in _profile_rows(user):
        table.add_row(label, _cell(value))
    return table


def _profile_rows(user: SpotifyUser) -> list[tuple[str, str | None]]:
    rows: list[tuple[str, str | None]] = [
        ("Display name", user.display_name),
        ("Spotify ID", user.id),
        ("Country", user.country),
        ("Subscription", _product_label(user.product)),
    ]
    if user.email:
        rows.append(("Email", user.email))
    if user.follower_count is not None:
        rows.append(("Followers", f"{user.follower_count:,}"))
    explicit = _explicit_label(user)
    if explicit is not None:
        rows.append(("Explicit filter", explicit))
    if user.profile_url:
        rows.append(("Profile", user.profile_url))
    if user.uri:
        rows.append(("URI", user.uri))
    return rows


def _product_label(product: str | None) -> str | None:
    if not product:
        return None
    return _PRODUCT_LABELS.get(product, product)


def _explicit_label(user: SpotifyUser) -> str | None:
    content = user.explicit_content
    if content is None:
        return None
    label = "On" if content.filter_enabled else "Off"
    if content.filter_locked:
        return f"{label}, locked"
    return label


def _cell(value: str | None) -> str:
    if not value:
        return "[dim]—[/dim]"
    return escape(value)


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
def profile() -> None:
    """Show the signed-in Spotify profile."""
    try:
        user = load_profile()
    except (AuthError, SpotifyAuthError) as exc:
        _stderr().print(_panel(str(exc), title="Not ready", style="yellow"))
        raise typer.Exit(code=1) from exc
    except SpotifyClientError as exc:
        _stderr().print(_panel(str(exc), title="Spotify", style="red"))
        raise typer.Exit(code=1) from exc
    _stdout().print(_profile_table(user))


@app.command()
def poll() -> None:
    """Poll recently-played and refresh saved tracks."""
    _call(run_poll)


@app.command()
def played() -> None:
    """Show tracks stored from recently played, newest first."""
    try:
        with session_scope() as session:
            rows = _load_played_tracks(session)
    except OperationalError as exc:
        detail = str(exc.orig) if exc.orig is not None else str(exc)
        _stderr().print(_panel(detail, title="Database unavailable", style="red"))
        raise typer.Exit(code=1) from exc

    console = _stdout(highlight=False)
    if rows:
        console.print(_played_table(rows, now=datetime.now(timezone.utc)))
    else:
        _empty(console, "Recently played", "No plays recorded yet.")


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
