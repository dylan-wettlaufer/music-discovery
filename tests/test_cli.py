import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from typer.testing import CliRunner

from music_discovery.cli import _load_played_tracks, _relative_phrase, app, load_profile
from music_discovery.models import PlayEvent, Track, User
from music_discovery.spotify.auth import AuthError
from music_discovery.spotify.client import SpotifyClientError, SpotifyUser

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(element, compiler, **kwargs):
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
def played_db():
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

    for table in (User.__table__, Track.__table__, PlayEvent.__table__):
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


def _track(track_id: str, title: str, artist: str) -> Track:
    return Track(
        spotify_track_id=track_id,
        name=title,
        primary_artist=artist,
        artist_ids=[f"artist-{track_id}"],
        normalized_key=f"{artist}:{title}".lower(),
    )


def test_help_lists_the_commands():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for name in ("auth", "profile", "poll", "played", "generate", "runs"):
        assert name in result.stdout
    assert "newest first" in result.stdout


def test_unwired_commands_exit():
    for args in (["generate"], ["generate", "--publish"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 1
        assert "not implemented" in result.output.lower()


def test_poll_invokes_the_history_job(monkeypatch):
    called: dict[str, bool] = {}

    def fake() -> None:
        called["poll"] = True

    monkeypatch.setattr("music_discovery.cli.run_poll", fake)
    result = runner.invoke(app, ["poll"])

    assert result.exit_code == 0
    assert called["poll"] is True


def test_auth_reports_missing_credentials(monkeypatch):
    from music_discovery.config import Settings

    monkeypatch.setattr(
        "music_discovery.spotify.auth.get_settings",
        lambda: Settings(
            _env_file=None,
            spotify_client_id="",
            spotify_client_secret="",
            token_encryption_key="",
        ),
    )

    result = runner.invoke(app, ["auth"])

    assert result.exit_code == 1
    assert "SPOTIFY_CLIENT_ID" in result.output
    assert "SPOTIFY_CLIENT_SECRET" in result.output
    assert "TOKEN_ENCRYPTION_KEY" in result.output


def test_relative_phrase_buckets_recent_plays():
    now = datetime(2026, 9, 29, 22, 0, tzinfo=timezone.utc)

    assert _relative_phrase(now - timedelta(seconds=10), now=now) == "just now"
    assert _relative_phrase(now - timedelta(minutes=12), now=now) == "12m ago"
    assert _relative_phrase(now - timedelta(hours=3), now=now) == "3h ago"
    assert _relative_phrase(now - timedelta(days=2), now=now) == "2d ago"
    assert _relative_phrase(now - timedelta(weeks=3), now=now) == "3w ago"
    assert _relative_phrase(now - timedelta(weeks=8), now=now) is None
    assert _relative_phrase(now + timedelta(minutes=5), now=now) is None


def test_played_lists_tracks_newest_first(played_db, monkeypatch):
    newest = datetime(2026, 9, 28, 18, 30, tzinfo=timezone.utc)
    middle = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    oldest = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
    with played_db() as session:
        user = User(spotify_user_id="spotify-user", refresh_token_encrypted="encrypted")
        session.add(user)
        session.flush()
        session.add_all(
            [
                _track("t1", "Idioteque", "Radiohead"),
                _track("t2", "Karma Police", "Radiohead"),
                _track("t3", "Treefingers", "Radiohead"),
            ]
        )
        session.flush()
        session.add_all(
            [
                PlayEvent(user_id=user.id, spotify_track_id="t1", played_at=oldest),
                PlayEvent(user_id=user.id, spotify_track_id="t2", played_at=middle),
                PlayEvent(user_id=user.id, spotify_track_id="t2", played_at=newest),
            ]
        )
        loaded = _load_played_tracks(session)

    def _stamp(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    assert [row.name for row in loaded] == ["Karma Police", "Karma Police", "Idioteque"]
    assert [row.primary_artist for row in loaded] == ["Radiohead", "Radiohead", "Radiohead"]
    assert [_stamp(row.played_at) for row in loaded] == [newest, middle, oldest]

    monkeypatch.setattr("music_discovery.cli.session_scope", played_db)
    result = runner.invoke(app, ["played"])

    assert result.exit_code == 0
    output = result.stdout
    assert output.index("Karma Police") < output.index("Idioteque")
    assert output.count("Karma Police") == 2
    assert "Treefingers" not in output
    assert "Radiohead" in output
    assert "3 plays" in output
    assert "newest first" in output
    assert "Artist" in output
    assert "Track" in output


def test_played_explains_when_nothing_is_stored(played_db, monkeypatch):
    monkeypatch.setattr("music_discovery.cli.session_scope", played_db)
    result = runner.invoke(app, ["played"])

    assert result.exit_code == 0
    assert "No plays recorded yet." in result.stdout


def test_played_reports_a_database_outage(monkeypatch):
    @contextmanager
    def unavailable() -> Iterator[Session]:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))
        yield

    monkeypatch.setattr("music_discovery.cli.session_scope", unavailable)
    result = runner.invoke(app, ["played"])

    assert result.exit_code == 1
    assert "connection refused" in result.output
    assert "Database unavailable" in result.output


def test_profile_prints_account_details(monkeypatch):
    payload = json.loads((FIXTURES / "spotify_me.json").read_text())
    monkeypatch.setattr(
        "music_discovery.cli.load_profile",
        lambda: SpotifyUser.model_validate(payload),
    )

    result = runner.invoke(app, ["profile"])

    assert result.exit_code == 0
    for text in (
        "Ada",
        "spotify-user",
        "US",
        "Premium",
        "ada@example.com",
        "12",
        "Off",
        "https://open.spotify.com/user/spotify-user",
        "spotify:user:spotify-user",
    ):
        assert text in result.stdout


def test_profile_prints_a_minimal_account(monkeypatch):
    monkeypatch.setattr(
        "music_discovery.cli.load_profile",
        lambda: SpotifyUser(id="spotify-user"),
    )

    result = runner.invoke(app, ["profile"])

    assert result.exit_code == 0
    assert "spotify-user" in result.stdout
    assert "ada@example.com" not in result.stdout


def test_profile_exits_when_sign_in_is_required(monkeypatch):
    def denied(**kwargs: object) -> str:
        del kwargs
        raise AuthError("Sign in with `music-discovery auth` first.")

    monkeypatch.setattr("music_discovery.cli.ensure_access_token", denied)

    result = runner.invoke(app, ["profile"])

    assert result.exit_code == 1
    assert "music-discovery auth" in result.output


def test_profile_reports_a_spotify_failure(monkeypatch):
    def failed() -> SpotifyUser:
        raise SpotifyClientError("Spotify request failed (503).")

    monkeypatch.setattr("music_discovery.cli.load_profile", failed)

    result = runner.invoke(app, ["profile"])

    assert result.exit_code == 1
    assert "503" in result.output


def test_load_profile_uses_the_me_endpoint(monkeypatch):
    monkeypatch.setattr(
        "music_discovery.cli.ensure_access_token",
        lambda **kwargs: "access-token",
    )
    payload = json.loads((FIXTURES / "spotify_me.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.spotify.com/v1/me"
        assert request.headers["authorization"] == "Bearer access-token"
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        user = load_profile(http_client=http)

    assert user.display_name == "Ada"
    assert user.id == "spotify-user"
