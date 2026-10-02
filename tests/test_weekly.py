import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from music_discovery.config import Settings
from music_discovery.db import Base
from music_discovery.jobs.weekly import WeeklyError, run_weekly
from music_discovery.models import JobRun, PlayEvent, RecommendationItem, RecommendationRun, Track, User
from music_discovery.normalize import normalized_key
from music_discovery.pipeline.publish import publish_playlist
from music_discovery.pipeline.seeds import SavedSeed, gather_seeds
from music_discovery.spotify.auth import AuthError
from music_discovery.spotify.client import SpotifyArtist, SpotifyClient, SpotifyTrack

_TODAY = date(2026, 10, 2)
_PLAYED = datetime(2026, 9, 29, 18, tzinfo=timezone.utc)


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
def weekly_db():
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

    Base.metadata.create_all(engine)

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


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        lastfm_api_key="lastfm-key",
        token_encryption_key=Fernet.generate_key().decode(),
        playlist_size=3,
        min_playlist_size=2,
        quota_similar_artist=1,
        quota_similar_track=1,
        quota_deep_cut=1,
        timezone="America/New_York",
    )


def _track(track_id: str, title: str, artist: str = "Radiohead") -> dict[str, object]:
    return {
        "id": track_id,
        "name": title,
        "popularity": 40,
        "artists": [{"id": "artist-radiohead", "name": artist}],
        "album": {"name": "Album", "album_type": "album", "release_date": "2024-01-01"},
    }


_CATALOG = {
    "track-karma": _track("track-karma", "Karma Police"),
    "track-karma-remaster": _track("track-karma-remaster", "Karma Police - 2017 Remastered"),
    "track-fishes": _track("track-fishes", "Weird Fishes"),
    "track-glory": _track("track-glory", "Glory Box", "Portishead"),
}


def _similar_payload(*rows: tuple[str, str, float]) -> dict[str, object]:
    return {
        "similartracks": {
            "track": [
                {"name": title, "artist": {"name": artist}, "match": match}
                for title, artist, match in rows
            ]
        }
    }


def _handler(*, publish: bool, posts: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ws.audioscrobbler.com":
            method = request.url.params["method"]
            if method == "artist.getSimilar":
                return httpx.Response(200, json={"similarartists": {"artist": []}})
            if method == "artist.getTopTracks":
                return httpx.Response(200, json={"toptracks": {"track": []}})
            if method == "track.getSimilar":
                return httpx.Response(
                    200,
                    json=_similar_payload(
                        ("Karma Police", "Radiohead", 5.0),
                        ("Karma Police - 2017 Remastered", "Radiohead", 4.0),
                        ("Weird Fishes", "Radiohead", 1.0),
                        ("Glory Box", "Portishead", 0.5),
                    ),
                )
            return httpx.Response(500, json={"message": method})
        if request.method != "GET":
            posts.append(f"{request.method} {request.url.path}")
            if not publish:
                return httpx.Response(500, json={"error": "dry run must not write"})
            if request.method == "POST":
                return httpx.Response(201, json={"id": "playlist-fresh", "name": "Fresh — Sep 27"})
            return httpx.Response(200, json={"snapshot_id": "snap"})
        path = request.url.path
        time_range = request.url.params.get("time_range")
        if path == "/v1/me/top/artists":
            items = []
            if time_range == "medium_term":
                items = [{"id": "artist-radiohead", "name": "Radiohead", "genres": ["art rock"]}]
            return httpx.Response(200, json={"items": items})
        if path == "/v1/me/top/tracks":
            items = []
            if time_range == "medium_term":
                items = [_CATALOG["track-karma"]]
            return httpx.Response(200, json={"items": items})
        if path.endswith("/albums"):
            return httpx.Response(200, json={"items": [], "next": None})
        if path == "/v1/search":
            query = request.url.params["q"]
            assert request.url.params["market"] == "US"
            title = query.split('track:"', 1)[1].split('"', 1)[0]
            chosen = next(track for track in _CATALOG.values() if track["name"] == title)
            return httpx.Response(200, json={"tracks": {"items": [chosen]}})
        if path.startswith("/v1/tracks/"):
            track_id = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=_CATALOG[track_id])
        if path.startswith("/v1/artists/") and not path.endswith("/albums"):
            return httpx.Response(
                200,
                json={"id": "artist-radiohead", "name": "Radiohead", "genres": ["art rock"]},
            )
        if path == "/v1/me/playlists":
            return httpx.Response(200, json={"items": [], "next": None})
        return httpx.Response(500, json={"path": path})

    return handler


def _seed_history(session: Session) -> None:
    user = User(
        spotify_user_id="spotify-user",
        country="US",
        refresh_token_encrypted="enc",
    )
    session.add(user)
    session.flush()
    session.add(
        Track(
            spotify_track_id="track-karma",
            name="Karma Police",
            primary_artist="Radiohead",
            artist_ids=["artist-radiohead"],
            normalized_key=normalized_key("Radiohead", "Karma Police"),
        )
    )
    session.flush()
    session.add(
        PlayEvent(
            user_id=user.id,
            spotify_track_id="track-karma",
            played_at=_PLAYED,
        )
    )


def test_a_played_song_and_its_remaster_are_absent(weekly_db, monkeypatch, caplog):
    monkeypatch.setattr(
        "music_discovery.jobs.weekly.ensure_access_token",
        lambda **kwargs: "token",
    )
    posts: list[str] = []
    with weekly_db() as session:
        _seed_history(session)
    http = httpx.Client(transport=httpx.MockTransport(_handler(publish=False, posts=posts)))
    with caplog.at_level(logging.INFO), http:
        result = run_weekly(
            publish=False,
            settings=_settings(),
            session_factory=weekly_db,
            http_client=http,
            sleep=lambda delay: None,
            today=_TODAY,
        )

    assert "Seeds ready: artists=1 tracks=1" in caplog.text
    assert "Similar artists 1/1 Radiohead" in caplog.text
    assert "Deep cuts ready:" in caplog.text
    assert "Dry run finished" in caplog.text
    selected = {item.candidate.spotify_track_id for item in result.selected}
    assert "track-karma" not in selected
    assert "track-karma-remaster" not in selected
    assert selected == {"track-fishes", "track-glory"}
    assert result.status == "dry_run"
    assert posts == []
    with weekly_db() as session:
        run = session.scalar(select(RecommendationRun))
        assert run is not None
        assert run.status == "dry_run"
        assert run.spotify_playlist_id is None
        assert run.week_start == date(2026, 9, 27)
        assert session.scalars(select(RecommendationItem)).all() == []
        job = session.scalar(select(JobRun).where(JobRun.job_name == "weekly_generate"))
        assert job is not None
        assert job.status == "succeeded"


def test_publish_creates_one_playlist_and_a_retry_adopts_the_week(weekly_db, monkeypatch):
    monkeypatch.setattr(
        "music_discovery.jobs.weekly.ensure_access_token",
        lambda **kwargs: "token",
    )
    posts: list[str] = []
    with weekly_db() as session:
        _seed_history(session)
    http = httpx.Client(transport=httpx.MockTransport(_handler(publish=True, posts=posts)))
    with http:
        first = run_weekly(
            publish=True,
            settings=_settings(),
            session_factory=weekly_db,
            http_client=http,
            sleep=lambda delay: None,
            today=_TODAY,
        )
        second = run_weekly(
            publish=True,
            settings=_settings(),
            session_factory=weekly_db,
            http_client=http,
            sleep=lambda delay: None,
            today=_TODAY,
        )

    assert first.playlist_id == "playlist-fresh"
    assert second.playlist_id == "playlist-fresh"
    assert posts == ["POST /v1/me/playlists", "PUT /v1/playlists/playlist-fresh/items"]
    with weekly_db() as session:
        items = session.scalars(select(RecommendationItem)).all()
        assert {item.spotify_track_id for item in items} == {"track-fishes", "track-glory"}
        assert "track-karma" not in {item.spotify_track_id for item in items}


def test_fewer_than_the_minimum_publishes_nothing(weekly_db, monkeypatch):
    monkeypatch.setattr(
        "music_discovery.jobs.weekly.ensure_access_token",
        lambda **kwargs: "token",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ws.audioscrobbler.com":
            if request.url.params["method"] == "track.getSimilar":
                return httpx.Response(
                    200,
                    json=_similar_payload(("Karma Police", "Radiohead", 5.0)),
                )
            return httpx.Response(200, json={"similarartists": {"artist": []}, "toptracks": {"track": []}})
        if request.method != "GET":
            return httpx.Response(500, json={"error": "must not publish"})
        if request.url.path == "/v1/me/top/artists":
            items = (
                [{"id": "artist-radiohead", "name": "Radiohead", "genres": ["art rock"]}]
                if request.url.params.get("time_range") == "medium_term"
                else []
            )
            return httpx.Response(200, json={"items": items})
        if request.url.path == "/v1/me/top/tracks":
            items = [_CATALOG["track-karma"]] if request.url.params.get("time_range") == "medium_term" else []
            return httpx.Response(200, json={"items": items})
        if request.url.path == "/v1/search":
            return httpx.Response(200, json={"tracks": {"items": [_CATALOG["track-karma"]]}})
        if request.url.path.startswith("/v1/tracks/"):
            return httpx.Response(200, json=_CATALOG["track-karma"])
        if request.url.path.startswith("/v1/artists/") and not request.url.path.endswith("/albums"):
            return httpx.Response(404, json={"error": {"status": 404}})
        if request.url.path.endswith("/albums"):
            return httpx.Response(200, json={"items": []})
        return httpx.Response(500, json={"path": request.url.path})

    with weekly_db() as session:
        _seed_history(session)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    with http:
        result = run_weekly(
            publish=True,
            settings=_settings(),
            session_factory=weekly_db,
            http_client=http,
            sleep=lambda delay: None,
            today=_TODAY,
        )

    assert result.status == "failed"
    assert result.playlist_id is None
    assert result.selected == ()
    with weekly_db() as session:
        run = session.scalar(select(RecommendationRun))
        assert run is not None
        assert run.status == "failed"
        assert run.spotify_playlist_id is None


def test_a_revoked_token_stops(weekly_db, monkeypatch):
    def denied(**kwargs: object) -> str:
        del kwargs
        raise AuthError("Spotify rejected the access token. Sign in with `music-discovery auth` again.")

    monkeypatch.setattr("music_discovery.jobs.weekly.ensure_access_token", denied)
    with weekly_db() as session:
        _seed_history(session)
    with pytest.raises(WeeklyError, match="auth"):
        run_weekly(
            publish=False,
            settings=_settings(),
            session_factory=weekly_db,
            http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
            sleep=lambda delay: None,
            today=_TODAY,
        )
    with weekly_db() as session:
        job = session.scalar(select(JobRun))
        assert job is not None
        assert job.status == "failed"
        assert session.scalar(select(RecommendationRun)) is None


def test_seed_caps_prefer_medium_term_and_sample_saved_tracks():
    artists = {
        "medium_term": [_artist(f"m{index}") for index in range(28)],
        "long_term": [_artist("long")],
        "short_term": [_artist(f"s{index}") for index in range(10)],
    }
    tracks = {
        "medium_term": [_spotify_track(f"t{index}") for index in range(30)],
        "long_term": [],
        "short_term": [_spotify_track(f"short{index}") for index in range(10)],
    }

    class Fake:
        def get_top_artists(self, *, time_range: str, limit: int = 50) -> list[SpotifyArtist]:
            del limit
            return artists[time_range]

        def get_top_tracks(self, *, time_range: str, limit: int = 50) -> list[SpotifyTrack]:
            del limit
            return tracks[time_range]

    saved = [
        SavedSeed(spotify_id=f"saved-{index}", artist="Radiohead", title=f"Cut {index}", artist_id=None)
        for index in range(10)
    ]
    seeds = gather_seeds(Fake(), saved)  # type: ignore[arg-type]

    assert len(seeds.artists) == 30
    assert seeds.artists[0].spotify_id == "m0"
    assert sum(1 for artist in seeds.artists if artist.spotify_id.startswith("s")) == 1
    assert len(seeds.tracks) == 40
    assert any(track.spotify_id.startswith("saved-") for track in seeds.tracks)


def test_publish_adopts_an_existing_playlist_name():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(500, json={"error": "should adopt"})
        if request.url.path == "/v1/me/playlists":
            return httpx.Response(
                200,
                json={"items": [{"id": "playlist-existing", "name": "Fresh — Sep 27"}]},
            )
        assert request.method == "PUT"
        return httpx.Response(200, json={"snapshot_id": "snap"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with http:
        playlist_id = publish_playlist(
            SpotifyClient("token", http_client=http),
            user_id="spotify-user",
            day=date(2026, 9, 27),
            track_ids=["track-fishes"],
        )

    assert playlist_id == "playlist-existing"
    assert calls == ["GET", "PUT"]


def _artist(artist_id: str) -> SpotifyArtist:
    return SpotifyArtist(id=artist_id, name=artist_id, genres=["art rock"])


def _spotify_track(track_id: str) -> SpotifyTrack:
    return SpotifyTrack(
        id=track_id,
        name=track_id,
        artists=[SpotifyArtist(id="artist-radiohead", name="Radiohead")],
    )
