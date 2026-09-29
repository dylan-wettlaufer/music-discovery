"""Typer commands: auth, poll, generate, runs."""

from collections.abc import Callable

import typer
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from music_discovery.db import session_scope
from music_discovery.jobs.poll_history import run_poll
from music_discovery.jobs.weekly import run_weekly
from music_discovery.models import JobRun, RecommendationRun
from music_discovery.pipeline.publish import publish_playlist
from music_discovery.spotify.auth import run_oauth

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Weekly private Spotify playlists of tracks you have never played.",
)


def _call(fn: Callable[[], None]) -> None:
    try:
        fn()
    except NotImplementedError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def auth() -> None:
    """One-time Spotify OAuth. Stores an encrypted refresh token."""
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
        typer.echo(f"Database unavailable: {exc.orig}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo("Jobs")
    if not job_rows:
        typer.echo("  (none)")
    for row in job_rows:
        gap = " gap" if row.gap_warning else ""
        error = f"  {row.error}" if row.error else ""
        typer.echo(
            f"  {row.id:>4}  {row.job_name:<22}  {row.status:<10}  "
            f"{row.started_at.isoformat(timespec='seconds')}{gap}{error}"
        )

    typer.echo("Recommendations")
    if not recommendation_rows:
        typer.echo("  (none)")
    for row in recommendation_rows:
        playlist = row.spotify_playlist_id or "-"
        typer.echo(f"  {row.id:>4}  {row.week_start}  {row.status:<10}  {playlist}")
