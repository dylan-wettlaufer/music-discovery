import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from music_discovery.config import Settings
from music_discovery.jobs.poll_history import (
    PollError,
    ingest_recently_played,
    ingest_saved_tracks,
    run_poll,
    run_recently_played,
    window_overflowed,
)
from music_discovery.spotify.auth import TOKEN_URL, AuthError, SpotifyProfile, save_connected_user
from music_discovery.models import JobRun, PlayEvent, SavedTrack, Track, User
from music_discovery.normalize import normalized_key
from music_discovery.spotify.client import (
    PlaybackContext,
    PlayedItem,
    SavedItem,
    SpotifyAlbum,
    SpotifyArtist,
    SpotifyTrack,
)

_START = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(element, compiler, **kwargs):
    del element, kwargs
    return "JSON"


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kwargs):
    del element, kwargs
    return "JSON"


def _dump_value(value: object) -> object:
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return value


def _dump_parameters(parameters: object) -> object:
    if isinstance(parameters, dict):
        return {key: _dump_value(value) for key, value in parameters.items()}
    if isinstance(parameters, tuple):
        return tuple(_dump_value(value) for value in parameters)
    if isinstance(parameters, list):
        return [_dump_parameters(value) for value in parameters]
    return parameters


@pytest.fixture
def history_db():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, connection_record) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _json_bind(conn, cursor, statement, parameters, context, executemany):
        del conn, cursor, context, executemany
        return statement, _dump_parameters(parameters)

    for table in (
        User.__table__,
        Track.__table__,
        PlayEvent.__table__,
        SavedTrack.__table__,
        JobRun.__table__,
    ):
        table.create(engine)

    @contextmanager
    def sessions() -> Iterator[Session]:
        session = Session(engine)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return sessions


def _user(session: Session, *, cursor: datetime | None = None) -> User:
    user = User(
        spotify_user_id="spotify-user",
        refresh_token_encrypted="encrypted",
        recently_played_cursor=cursor,
    )
    session.add(user)
    session.flush()
    return user


def _track(
    track_id: str,
    title: str,
    artist: str,
    *,
    popularity: int = 10,
) -> SpotifyTrack:
    return SpotifyTrack(
        id=track_id,
        name=title,
        popularity=popularity,
        duration_ms=180000,
        artists=[SpotifyArtist(id=f"artist-{artist}", name=artist)],
        album=SpotifyAlbum(name="OK Computer", release_date="1997-06-16"),
    )


def _played(
    track_id: str,
    title: str,
    artist: str,
    played_at: datetime,
    *,
    popularity: int = 10,
    context_uri: str | None = None,
) -> PlayedItem:
    return PlayedItem(
        track=_track(track_id, title, artist, popularity=popularity),
        played_at=played_at,
        context=PlaybackContext(uri=context_uri) if context_uri else None,
    )


def _saved(track_id: str, title: str, artist: str, added_at: datetime) -> SavedItem:
    return SavedItem(
        added_at=added_at,
        track=_track(track_id, title, artist),
    )


def _stamps(count: int, *, start: datetime = _START) -> list[datetime]:
    """Oldest is not at either end, so min/max cannot trust item order."""
    ordered = [start + timedelta(minutes=index) for index in range(count)]
    oldest = ordered[0]
    newest = ordered[-1]
    middle = ordered[1:-1]
    return middle[:5] + [newest, oldest] + middle[5:]


def test_first_full_window_is_not_a_gap():
    played_at = _stamps(50)
    assert window_overflowed(played_at, previous_cursor=None) is False


def test_full_window_newer_than_the_cursor_is_a_gap():
    cursor = _START
    played_at = _stamps(50, start=cursor + timedelta(minutes=1))
    assert window_overflowed(played_at, previous_cursor=cursor) is True


def test_oldest_item_still_at_the_cursor_is_not_a_gap():
    cursor = _START
    played_at = _stamps(50, start=cursor)
    assert min(played_at) == cursor
    assert window_overflowed(played_at, previous_cursor=cursor) is False


def test_recently_played_sets_the_cursor_from_the_newest_play(history_db):
    played_at = _stamps(3)
    items = [
        _played(f"track-{index}", "Karma Police", "Radiohead", stamp, context_uri="spotify:playlist:fresh")
        for index, stamp in enumerate(played_at)
    ]
    with history_db() as session:
        user = _user(session)
        stats = ingest_recently_played(session, user, items)

    assert stats.gap is False
    assert stats.inserted == 3
    assert stats.cursor == max(played_at)
    with history_db() as session:
        stored = session.scalar(select(User))
        assert stored is not None
        assert _aware(stored.recently_played_cursor) == max(played_at)
        play = session.scalar(select(PlayEvent))
        assert play is not None
        assert play.context_uri == "spotify:playlist:fresh"


def test_replaying_the_same_window_does_not_duplicate_plays(history_db):
    items = [_played("track-karma", "Karma Police", "Radiohead", _START)]
    with history_db() as session:
        user = _user(session)
        first = ingest_recently_played(session, user, items)
        second = ingest_recently_played(session, user, items)
        assert first.inserted == 1
        assert second.inserted == 0
        assert session.scalar(select(func.count()).select_from(PlayEvent)) == 1
        assert session.scalar(select(func.count()).select_from(Track)) == 1


def test_a_remaster_keeps_its_own_id_and_the_same_normalized_key(history_db):
    items = [
        _played("track-karma", "Karma Police", "Radiohead", _START),
        _played(
            "track-karma-remaster",
            "Karma Police (Remastered)",
            "Radiohead",
            _START + timedelta(days=1),
        ),
    ]
    with history_db() as session:
        user = _user(session)
        ingest_recently_played(session, user, items)
        rows = session.scalars(select(Track)).all()
        ids = {row.spotify_track_id for row in rows}
        keys = {row.normalized_key for row in rows}

    assert ids == {"track-karma", "track-karma-remaster"}
    assert keys == {normalized_key("Radiohead", "Karma Police")}


def test_a_later_sighting_updates_track_fields(history_db):
    with history_db() as session:
        user = _user(session)
        ingest_recently_played(
            session,
            user,
            [_played("track-karma", "Karma Police", "Radiohead", _START, popularity=10)],
        )
        ingest_recently_played(
            session,
            user,
            [
                _played(
                    "track-karma",
                    "Karma Police",
                    "Radiohead",
                    _START + timedelta(hours=1),
                    popularity=80,
                )
            ],
        )
        stored = session.get(Track, "track-karma")
        popularity = stored.popularity if stored is not None else None
        plays = session.scalar(select(func.count()).select_from(PlayEvent))

    assert popularity == 80
    assert plays == 2


def test_local_files_and_tracks_without_an_artist_are_skipped(history_db):
    items = [
        PlayedItem(
            track=SpotifyTrack(id=None, name="Bedroom demo", artists=[SpotifyArtist(name="Local")]),
            played_at=_START,
        ),
        PlayedItem(
            track=SpotifyTrack(id="track-unknown", name="Untitled", artists=[]),
            played_at=_START + timedelta(minutes=1),
        ),
        _played("track-karma", "Karma Police", "Radiohead", _START + timedelta(minutes=2)),
    ]
    with history_db() as session:
        user = _user(session)
        stats = ingest_recently_played(session, user, items)
        assert stats.skipped_local == 1
        assert stats.skipped == 1
        assert stats.inserted == 1
        assert session.scalar(select(func.count()).select_from(Track)) == 1


def test_saved_tracks_are_not_duplicated_or_removed(history_db):
    first = [
        _saved("track-karma", "Karma Police", "Radiohead", _START),
        _saved("track-fishes", "Weird Fishes", "Radiohead", _START + timedelta(days=1)),
    ]
    second = [_saved("track-karma", "Karma Police", "Radiohead", _START + timedelta(days=2))]
    with history_db() as session:
        user = _user(session)
        added = ingest_saved_tracks(session, user, first)
        again = ingest_saved_tracks(session, user, second)
        assert added.inserted == 2
        assert again.inserted == 0
        ids = set(session.scalars(select(SavedTrack.spotify_track_id)).all())

    assert ids == {"track-karma", "track-fishes"}


def test_recently_played_job_records_a_gap(history_db):
    settings = _settings()
    cursor = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with history_db() as session:
        _connect(session, settings, expires_in=timedelta(hours=2))
        user = session.scalar(select(User))
        assert user is not None
        user.recently_played_cursor = cursor

    def handler(request: httpx.Request) -> httpx.Response:
        assert "after" not in request.url.params
        return httpx.Response(200, json=_window_payload(cursor + timedelta(minutes=1)))

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with http:
        stats = run_recently_played(
            settings=settings,
            session_factory=history_db,
            http_client=http,
        )

    assert stats.gap is True
    assert stats.inserted == 50
    with history_db() as session:
        job = session.scalar(select(JobRun))
        assert job is not None
        assert job.job_name == "recently_played"
        assert job.status == "succeeded"
        assert job.gap_warning is True
        assert job.details is not None
        assert job.details["inserted"] == 50


def test_poll_reports_both_ingests(history_db):
    settings = _settings()
    with history_db() as session:
        _connect(session, settings, expires_in=timedelta(hours=2))
    saved = {
        "items": [
            {
                "added_at": "2026-01-02T00:00:00Z",
                "track": {
                    "id": "track-saved",
                    "name": "Weird Fishes",
                    "artists": [{"id": "artist-radiohead", "name": "Radiohead"}],
                    "album": {"name": "In Rainbows", "release_date": "2007-10-10"},
                },
            }
        ],
        "next": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/recently-played"):
            return httpx.Response(200, json=_window_payload(_START, count=1))
        return httpx.Response(200, json=saved)

    lines: list[str] = []
    http = httpx.Client(transport=httpx.MockTransport(handler))
    with http:
        run_poll(
            settings=settings,
            session_factory=history_db,
            http_client=http,
            echo=lines.append,
        )

    assert lines == [
        "Recently played: 1 seen, 1 new.",
        "Saved tracks: 1 seen, 1 new.",
    ]
    with history_db() as session:
        names = set(session.scalars(select(JobRun.job_name)).all())
    assert names == {"recently_played", "saved_tracks"}


def test_revoked_refresh_token_stops_before_saved_tracks(history_db):
    settings = _settings()
    with history_db() as session:
        _connect(session, settings, expires_in=timedelta(hours=2))
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url == TOKEN_URL:
            return httpx.Response(
                400,
                json={"error": "invalid_grant", "error_description": "Refresh token revoked"},
            )
        return httpx.Response(401, json={"error": {"status": 401, "message": "The access token expired"}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with http:
        with pytest.raises(AuthError, match="music-discovery auth"):
            run_poll(settings=settings, session_factory=history_db, http_client=http)

    assert any(path.endswith("/recently-played") for path in requested)
    assert not any(path.endswith("/tracks") for path in requested)
    with history_db() as session:
        job = session.scalar(select(JobRun))
        assert job is not None
        assert job.status == "failed"
        assert job.job_name == "recently_played"


def test_spotify_failure_is_recorded(history_db):
    settings = _settings()
    with history_db() as session:
        _connect(session, settings, expires_in=timedelta(hours=2))

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={"error": "server"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with http:
        with pytest.raises(PollError, match="500"):
            run_recently_played(settings=settings, session_factory=history_db, http_client=http)

    with history_db() as session:
        job = session.scalar(select(JobRun))
        plays = session.scalar(select(func.count()).select_from(PlayEvent))
        assert job is not None
        assert job.status == "failed"
        assert plays == 0


def test_poll_requires_a_sign_in(history_db):
    with pytest.raises(AuthError, match="music-discovery auth"):
        run_recently_played(settings=_settings(), session_factory=history_db)

    with history_db() as session:
        job = session.scalar(select(JobRun))
        assert job is not None
        assert job.status == "failed"


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        spotify_client_id="client-id",
        spotify_client_secret="client-secret",
        token_encryption_key=Fernet.generate_key().decode(),
        database_url="sqlite://",
    )


def _connect(session: Session, settings: Settings, *, expires_in: timedelta) -> None:
    save_connected_user(
        session,
        profile=SpotifyProfile(id="spotify-user", country="US", display_name="Ada"),
        refresh_token="refresh-token",
        access_token="access-token",
        expires_at=datetime.now(timezone.utc) + expires_in,
        encryption_key=settings.token_encryption_key,
    )


def _window_payload(start: datetime, *, count: int = 50) -> dict[str, object]:
    items = []
    for index in range(count):
        played = start + timedelta(minutes=index)
        items.append(
            {
                "track": {
                    "id": f"track-{index}",
                    "name": "Karma Police",
                    "duration_ms": 1000,
                    "popularity": 1,
                    "artists": [{"id": "artist-radiohead", "name": "Radiohead"}],
                    "album": {"name": "OK Computer", "release_date": "1997-06-16"},
                },
                "played_at": played.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "context": None,
            }
        )
    return {"items": items}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
