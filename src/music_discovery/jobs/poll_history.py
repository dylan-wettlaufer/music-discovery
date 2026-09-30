"""Recently-played poll and saved-track ingest."""

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from music_discovery.config import Settings, get_settings
from music_discovery.db import session_scope
from music_discovery.models import JobRun, PlayEvent, SavedTrack, Track, User
from music_discovery.normalize import normalized_key
from music_discovery.spotify.auth import AuthError, SessionFactory, ensure_access_token
from music_discovery.spotify.client import (
    RECENTLY_PLAYED_LIMIT,
    PlayedItem,
    SavedItem,
    SpotifyAuthError,
    SpotifyClient,
    SpotifyClientError,
    SpotifyTrack,
)

logger = logging.getLogger("music_discovery.jobs.poll_history")

JOB_RECENTLY_PLAYED = "recently_played"
JOB_SAVED_TRACKS = "saved_tracks"


@dataclass(frozen=True)
class IngestStats:
    fetched: int
    inserted: int
    skipped_local: int
    skipped: int
    gap: bool = False
    cursor: datetime | None = None

    def details(self) -> dict[str, object]:
        body: dict[str, object] = {
            "fetched": self.fetched,
            "inserted": self.inserted,
            "skipped_local": self.skipped_local,
            "skipped": self.skipped,
        }
        if self.gap:
            body["gap"] = True
        if self.cursor is not None:
            body["cursor"] = self.cursor.isoformat()
        return body


def window_overflowed(played_at: Sequence[datetime], previous_cursor: datetime | None) -> bool:
    """True when the 50-play window no longer reaches the previous cursor."""
    if previous_cursor is None or len(played_at) < RECENTLY_PLAYED_LIMIT:
        return False
    oldest = min(_as_utc(value) for value in played_at)
    return oldest > _as_utc(previous_cursor)


def ingest_recently_played(
    session: Session,
    user: User,
    items: Sequence[PlayedItem],
) -> IngestStats:
    """Upsert tracks, insert plays, and advance the cursor in this transaction."""
    session.flush()
    gap = window_overflowed([item.played_at for item in items], user.recently_played_cursor)
    inserted = 0
    skipped_local = 0
    skipped = 0
    for item in items:
        fields, kind = _catalog_fields(item.track)
        if kind == "local":
            skipped_local += 1
            continue
        if fields is None:
            skipped += 1
            continue
        _upsert_track(session, fields)
        if _insert_play(
            session,
            user_id=user.id,
            spotify_track_id=str(fields["spotify_track_id"]),
            played_at=_as_utc(item.played_at),
            context_uri=item.context_uri,
        ):
            inserted += 1
    cursor = _as_utc(user.recently_played_cursor) if user.recently_played_cursor else None
    if items:
        cursor = max(_as_utc(item.played_at) for item in items)
        user.recently_played_cursor = cursor
    return IngestStats(
        fetched=len(items),
        inserted=inserted,
        skipped_local=skipped_local,
        skipped=skipped,
        gap=gap,
        cursor=cursor,
    )


def ingest_saved_tracks(
    session: Session,
    user: User,
    items: Sequence[SavedItem],
) -> IngestStats:
    """Upsert tracks and insert library rows. Rows are not removed when unsaved."""
    session.flush()
    inserted = 0
    skipped_local = 0
    skipped = 0
    for item in items:
        fields, kind = _catalog_fields(item.track)
        if kind == "local":
            skipped_local += 1
            continue
        if fields is None:
            skipped += 1
            continue
        _upsert_track(session, fields)
        added_at = _as_utc(item.added_at) if item.added_at is not None else None
        if _insert_saved(
            session,
            user_id=user.id,
            spotify_track_id=str(fields["spotify_track_id"]),
            added_at=added_at,
        ):
            inserted += 1
    return IngestStats(
        fetched=len(items),
        inserted=inserted,
        skipped_local=skipped_local,
        skipped=skipped,
    )


class PollError(Exception):
    """Recently-played or saved-track ingest failed."""


def run_recently_played(
    *,
    settings: Settings | None = None,
    session_factory: SessionFactory | None = None,
    http_client: httpx.Client | None = None,
    sleep: Callable[[float], None] | None = None,
) -> IngestStats:
    """Append plays idempotently and warn when the 50-track window overflowed."""
    return _run_job(
        JOB_RECENTLY_PLAYED,
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
        JOB_SAVED_TRACKS,
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
    echo: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Manual poll: recently played, then saved tracks.

    A revoked token stops after recently-played. The daily saved-track schedule
    still runs on its own.
    """
    echo = echo or print
    recent = run_recently_played(
        settings=settings,
        session_factory=session_factory,
        http_client=http_client,
        sleep=sleep,
    )
    echo(_recent_summary(recent))
    saved = run_saved_tracks(
        settings=settings,
        session_factory=session_factory,
        http_client=http_client,
        sleep=sleep,
    )
    echo(_saved_summary(saved))


def _run_job(
    job_name: str,
    fetch: Callable[[SpotifyClient], Sequence[PlayedItem] | Sequence[SavedItem]],
    ingest: Callable[[Session, User, Sequence[PlayedItem] | Sequence[SavedItem]], IngestStats],
    *,
    settings: Settings | None,
    session_factory: SessionFactory | None,
    http_client: httpx.Client | None,
    sleep: Callable[[float], None] | None,
) -> IngestStats:
    settings = settings or get_settings()
    started = datetime.now(timezone.utc)
    owns_client = http_client is None
    http = http_client or httpx.Client(timeout=30)
    try:
        client = _spotify_client(settings, session_factory, http, sleep)
        items = fetch(client)
        with _sessions(session_factory) as session:
            user = session.scalar(select(User).order_by(User.id).limit(1))
            if user is None:
                raise AuthError("Sign in with `music-discovery auth` before polling.")
            stats = ingest(session, user, items)
            session.add(_job_row(job_name, started, status="succeeded", stats=stats))
        logger.info(
            "%s fetched=%s inserted=%s gap=%s",
            job_name,
            stats.fetched,
            stats.inserted,
            stats.gap,
        )
        return stats
    except Exception as exc:
        reported = _poll_error(exc)
        _record_failure(job_name, started, reported, session_factory)
        if reported is exc:
            raise
        raise reported from exc
    finally:
        if owns_client:
            http.close()


def _spotify_client(
    settings: Settings,
    session_factory: SessionFactory | None,
    http: httpx.Client,
    sleep: Callable[[float], None] | None,
) -> SpotifyClient:
    holder = {
        "token": ensure_access_token(
            settings=settings,
            http_client=http,
            session_factory=session_factory,
        )
    }

    def token() -> str:
        return holder["token"]

    def refresh() -> str:
        holder["token"] = ensure_access_token(
            settings=settings,
            http_client=http,
            session_factory=session_factory,
            force=True,
        )
        return holder["token"]

    return SpotifyClient(token, http_client=http, refresh=refresh, sleep=sleep)


def _poll_error(exc: Exception) -> Exception:
    if isinstance(exc, SpotifyAuthError):
        return AuthError(str(exc))
    if isinstance(exc, AuthError):
        return exc
    if isinstance(exc, OperationalError):
        return AuthError(f"Database unavailable: {exc.orig}")
    if isinstance(exc, (PollError, SpotifyClientError)):
        return PollError(str(exc))
    return PollError(str(exc))


def _record_failure(
    job_name: str,
    started: datetime,
    exc: Exception,
    session_factory: SessionFactory | None,
) -> None:
    try:
        with _sessions(session_factory) as session:
            session.add(_job_row(job_name, started, status="failed", error=str(exc)))
    except Exception:
        logger.exception("Could not record %s failure", job_name)


def _job_row(
    job_name: str,
    started: datetime,
    *,
    status: str,
    stats: IngestStats | None = None,
    error: str | None = None,
) -> JobRun:
    return JobRun(
        job_name=job_name,
        started_at=started,
        finished_at=datetime.now(timezone.utc),
        status=status,
        error=error,
        gap_warning=bool(stats and stats.gap),
        details=stats.details() if stats is not None else None,
    )


def _recent_summary(stats: IngestStats) -> str:
    message = f"Recently played: {stats.fetched} seen, {stats.inserted} new."
    if stats.gap:
        message += " The 50-play window overflowed; some plays were missed."
    return message


def _saved_summary(stats: IngestStats) -> str:
    return f"Saved tracks: {stats.fetched} seen, {stats.inserted} new."


@contextmanager
def _sessions(session_factory: SessionFactory | None) -> Iterator[Session]:
    factory = session_factory or session_scope
    with factory() as session:
        yield session


def _catalog_fields(track: SpotifyTrack | None) -> tuple[Mapping[str, object] | None, str]:
    if track is None or not track.id:
        return None, "local"
    artist = track.artists[0].name.strip() if track.artists else ""
    title = track.name.strip()
    if not artist or not title:
        return None, "skipped"
    album = track.album
    return {
        "spotify_track_id": track.id,
        "name": title,
        "primary_artist": artist,
        "artist_ids": [row.id for row in track.artists if row.id],
        "album_name": album.name if album is not None and album.name else None,
        "duration_ms": track.duration_ms,
        "release_date": album.release_date if album is not None else None,
        "popularity": track.popularity,
        "normalized_key": normalized_key(artist, title),
    }, "ok"


def _upsert_track(session: Session, fields: Mapping[str, object]) -> None:
    insert = _dialect_insert(session)
    stmt = insert(Track).values(**fields)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Track.spotify_track_id],
            set_={
                "name": stmt.excluded.name,
                "primary_artist": stmt.excluded.primary_artist,
                "artist_ids": stmt.excluded.artist_ids,
                "album_name": stmt.excluded.album_name,
                "duration_ms": stmt.excluded.duration_ms,
                "release_date": stmt.excluded.release_date,
                "popularity": stmt.excluded.popularity,
                "normalized_key": stmt.excluded.normalized_key,
            },
        )
    )


def _insert_play(
    session: Session,
    *,
    user_id: int,
    spotify_track_id: str,
    played_at: datetime,
    context_uri: str | None,
) -> bool:
    insert = _dialect_insert(session)
    result = session.execute(
        insert(PlayEvent)
        .values(
            user_id=user_id,
            spotify_track_id=spotify_track_id,
            played_at=played_at,
            context_uri=context_uri,
        )
        .on_conflict_do_nothing(
            index_elements=["user_id", "spotify_track_id", "played_at"],
        )
        .returning(PlayEvent.id)
    )
    # Postgres reports rowcount 0 for INSERT ON CONFLICT, including new rows.
    return result.scalar_one_or_none() is not None


def _insert_saved(
    session: Session,
    *,
    user_id: int,
    spotify_track_id: str,
    added_at: datetime | None,
) -> bool:
    insert = _dialect_insert(session)
    result = session.execute(
        insert(SavedTrack)
        .values(user_id=user_id, spotify_track_id=spotify_track_id, added_at=added_at)
        .on_conflict_do_nothing(index_elements=["user_id", "spotify_track_id"])
        .returning(SavedTrack.id)
    )
    return result.scalar_one_or_none() is not None


def _dialect_insert(session: Session):
    name = session.get_bind().dialect.name
    if name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError(f"Unsupported database dialect: {name}")
    return insert


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
