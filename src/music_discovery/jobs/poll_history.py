"""Recently-played poll and saved-track ingest.

Plays and saved tracks are lifetime excludes. A retry inserts nothing new.
The recently-played cursor moves only after the rows are in the session, and
the caller commits that session together with the cursor.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Insert
from sqlalchemy.orm import Session

from music_discovery.models import PlayEvent, SavedTrack, Track, User
from music_discovery.normalize import normalized_key
from music_discovery.spotify.client import PlayedItem, RecentlyPlayed, SavedItem, SavedTracks, SpotifyTrack

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


def run_recently_played() -> None:
    """Append plays idempotently and warn when the 50-track window overflowed."""
    raise NotImplementedError("Recently-played poll is not implemented.")


def run_saved_tracks() -> None:
    """Refresh the library. Saved tracks are seeds and lifetime hard excludes."""
    raise NotImplementedError("Saved-track ingest is not implemented.")


def run_poll() -> None:
    """Manual poll: recently played, then saved tracks."""
    run_recently_played()
    run_saved_tracks()


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
