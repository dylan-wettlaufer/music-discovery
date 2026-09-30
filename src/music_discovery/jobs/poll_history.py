"""Recently-played poll and saved-track ingest.

Plays and saved tracks are lifetime excludes. A retry inserts nothing new.
The recently-played cursor moves only after the rows are in the session, and
the caller commits that session together with the cursor.
"""

import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import Insert, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from music_discovery.config import Settings, get_settings
from music_discovery.db import session_scope
from music_discovery.models import JobRun, PlayEvent, SavedTrack, Track, User
from music_discovery.normalize import normalized_key
from music_discovery.spotify.auth import AuthError, SessionFactory, load_access_token
from music_discovery.spotify.client import (
    PlayedItem,
    RecentlyPlayed,
    SavedItem,
    SavedTracks,
    SpotifyClient,
    SpotifyTrack,
)

logger = logging.getLogger("music_discovery.jobs.poll_history")

_WINDOW_LIMIT = 50


@dataclass(frozen=True)
class IngestStats:
    fetched: int
    inserted: int
    skipped: int
    gap_warning: bool
    cursor: datetime | None


def detect_gap(
    item_count: int,
    played_at: Sequence[datetime],
    previous_cursor: datetime | None,
) -> bool:
    """True when a full window is entirely newer than the last poll.

    The first poll has no cursor, so a full window is the start of history.
    ``before`` and ``after`` only slice this same window, so a gap cannot be
    filled by paging.
    """
    if previous_cursor is None or item_count != _WINDOW_LIMIT or not played_at:
        return False
    oldest = min(_as_utc(value) for value in played_at)
    return oldest > _as_utc(previous_cursor)


def ingest_recently_played(session: Session, user: User, page: RecentlyPlayed) -> IngestStats:
    """Upsert tracks, insert plays, and advance the cursor to the newest play."""
    previous = user.recently_played_cursor
    inserted = 0
    skipped = page.skipped_local
    for item in page.items:
        if _store_play(session, user, item):
            inserted += 1
        elif item.track is not None and not _can_store(item.track):
            skipped += 1
    played_at = [item.played_at for item in page.items]
    newest = max((_as_utc(value) for value in played_at), default=None)
    if newest is not None and (previous is None or newest > _as_utc(previous)):
        user.recently_played_cursor = newest
    return IngestStats(
        fetched=page.item_count,
        inserted=inserted,
        skipped=skipped,
        gap_warning=detect_gap(page.item_count, played_at, previous),
        cursor=user.recently_played_cursor,
    )


def ingest_saved_tracks(session: Session, user: User, saved: SavedTracks) -> IngestStats:
    """Upsert the library. Rows that leave the library stay excluded."""
    inserted = 0
    skipped = saved.skipped_local
    for item in saved.items:
        if _store_saved(session, user, item):
            inserted += 1
        elif item.track is not None and not _can_store(item.track):
            skipped += 1
    return IngestStats(
        fetched=saved.fetched,
        inserted=inserted,
        skipped=skipped,
        gap_warning=False,
        cursor=None,
    )


def run_recently_played(
    *,
    settings: Settings | None = None,
    session_factory: SessionFactory | None = None,
    http_client: httpx.Client | None = None,
    sleep: Callable[[float], None] | None = None,
) -> IngestStats:
    """Append plays idempotently and warn when the 50-track window overflowed."""
    return _run_job(
        "recently_played",
        lambda client: client.get_recently_played(),
        ingest_recently_played,
        settings=settings,
        session_factory=session_factory,
        http_client=http_client,
        sleep=sleep,
    )


def run_saved_tracks(
    *,
    settings: Settings | None = None,
    session_factory: SessionFactory | None = None,
    http_client: httpx.Client | None = None,
    sleep: Callable[[float], None] | None = None,
) -> IngestStats:
    """Refresh the library. Saved tracks are seeds and lifetime hard excludes."""
    return _run_job(
        "saved_tracks",
        lambda client: client.get_saved_tracks(),
        ingest_saved_tracks,
        settings=settings,
        session_factory=session_factory,
        http_client=http_client,
        sleep=sleep,
    )


def run_poll(
    *,
    settings: Settings | None = None,
    session_factory: SessionFactory | None = None,
    http_client: httpx.Client | None = None,
    sleep: Callable[[float], None] | None = None,
) -> str:
    """Poll recently played, then saved tracks. A revoked token stops there."""
    recent = run_recently_played(
        settings=settings,
        session_factory=session_factory,
        http_client=http_client,
        sleep=sleep,
    )
    saved = run_saved_tracks(
        settings=settings,
        session_factory=session_factory,
        http_client=http_client,
        sleep=sleep,
    )
    gap = ""
    if recent.gap_warning:
        gap = " Gap: the 50-play window moved past the last cursor."
    return f"Recently played: {recent.inserted} new.{gap}\nSaved tracks: {saved.inserted} new."


def _run_job(
    job_name: str,
    fetch: Callable[[SpotifyClient], Any],
    ingest: Callable[[Session, User, Any], IngestStats],
    *,
    settings: Settings | None,
    session_factory: SessionFactory | None,
    http_client: httpx.Client | None,
    sleep: Callable[[float], None] | None,
) -> IngestStats:
    started = datetime.now(timezone.utc)
    try:
        page = _fetch(fetch, settings=settings, session_factory=session_factory, http_client=http_client, sleep=sleep)
        with _sessions(session_factory) as session:
            stats = ingest(session, _require_user(session), page)
    except Exception as exc:
        _record_job(
            job_name,
            started,
            status="failed",
            error=str(exc),
            gap_warning=False,
            details=None,
            session_factory=session_factory,
        )
        raise
    _record_job(
        job_name,
        started,
        status="succeeded",
        error=None,
        gap_warning=stats.gap_warning,
        details={"fetched": stats.fetched, "inserted": stats.inserted, "skipped": stats.skipped},
        session_factory=session_factory,
    )
    logger.info(
        "%s fetched=%s inserted=%s skipped=%s gap=%s",
        job_name,
        stats.fetched,
        stats.inserted,
        stats.skipped,
        stats.gap_warning,
    )
    return stats


def _fetch(
    fetch: Callable[[SpotifyClient], Any],
    *,
    settings: Settings | None,
    session_factory: SessionFactory | None,
    http_client: httpx.Client | None,
    sleep: Callable[[float], None] | None,
) -> Any:
    settings = settings or get_settings()
    owns_http = http_client is None
    http = http_client or httpx.Client(timeout=30)
    try:
        token = load_access_token(
            settings=settings,
            http_client=http,
            session_factory=session_factory,
        )

        def refresh() -> str:
            return load_access_token(
                settings=settings,
                http_client=http,
                session_factory=session_factory,
                force_refresh=True,
            )

        client = SpotifyClient(token, http_client=http, refresh=refresh, sleep=sleep)
        try:
            return fetch(client)
        finally:
            client.close()
    finally:
        if owns_http:
            http.close()


def _require_user(session: Session) -> User:
    user = session.scalar(select(User).order_by(User.id).limit(1))
    if user is None:
        raise AuthError("Sign in with `music-discovery auth` before polling.")
    return user


def _record_job(
    job_name: str,
    started: datetime,
    *,
    status: str,
    error: str | None,
    gap_warning: bool,
    details: dict[str, int] | None,
    session_factory: SessionFactory | None,
) -> None:
    try:
        with _sessions(session_factory) as session:
            session.add(
                JobRun(
                    job_name=job_name,
                    started_at=started,
                    finished_at=datetime.now(timezone.utc),
                    status=status,
                    error=error,
                    gap_warning=gap_warning,
                    details=details,
                )
            )
    except OperationalError as exc:
        raise AuthError(f"Database unavailable: {exc.orig}") from exc


@contextmanager
def _sessions(session_factory: SessionFactory | None) -> Iterator[Session]:
    factory = session_factory or session_scope
    with factory() as session:
        yield session


def _store_play(session: Session, user: User, item: PlayedItem) -> bool:
    track = item.track
    if track is None or not _can_store(track):
        return False
    _upsert_track(session, track)
    statement = _insert(session, PlayEvent).values(
        user_id=user.id,
        spotify_track_id=track.id,
        played_at=_as_utc(item.played_at),
        context_uri=item.context_uri,
    )
    result = session.execute(
        statement.on_conflict_do_nothing(
            index_elements=["user_id", "spotify_track_id", "played_at"]
        )
    )
    return result.rowcount == 1


def _store_saved(session: Session, user: User, item: SavedItem) -> bool:
    track = item.track
    if track is None or not _can_store(track):
        return False
    _upsert_track(session, track)
    added_at = _as_utc(item.added_at) if item.added_at is not None else None
    statement = _insert(session, SavedTrack).values(
        user_id=user.id,
        spotify_track_id=track.id,
        added_at=added_at,
    )
    result = session.execute(
        statement.on_conflict_do_nothing(index_elements=["user_id", "spotify_track_id"])
    )
    return result.rowcount == 1


def _can_store(track: SpotifyTrack) -> bool:
    return bool(track.name.strip()) and bool(track.primary_artist.strip())


def _upsert_track(session: Session, track: SpotifyTrack) -> None:
    values = {
        "spotify_track_id": track.id,
        "name": track.name,
        "primary_artist": track.primary_artist,
        "artist_ids": list(track.artist_ids),
        "album_name": track.album_name,
        "duration_ms": track.duration_ms,
        "release_date": track.release_date,
        "popularity": track.popularity,
        "normalized_key": normalized_key(track.primary_artist, track.name),
    }
    statement = _insert(session, Track).values(**values)
    excluded = statement.excluded
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[Track.spotify_track_id],
            set_={
                "name": excluded.name,
                "primary_artist": excluded.primary_artist,
                "artist_ids": excluded.artist_ids,
                "album_name": excluded.album_name,
                "duration_ms": excluded.duration_ms,
                "release_date": excluded.release_date,
                "popularity": excluded.popularity,
                "normalized_key": excluded.normalized_key,
            },
        )
    )


def _insert(session: Session, model: type[Track] | type[PlayEvent] | type[SavedTrack]) -> Insert:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError(f"Unsupported database dialect: {dialect}")
    return insert(model)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
