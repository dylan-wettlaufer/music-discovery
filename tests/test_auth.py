import json
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from music_discovery.config import Settings
from music_discovery.crypto import decrypt_token
from music_discovery.models import User
from music_discovery.spotify.auth import (
    PROFILE_URL,
    TOKEN_URL,
    AuthError,
    SpotifyProfile,
    ensure_access_token,
    exchange_authorization_code,
    fetch_profile,
    refresh_access_token,
    run_oauth,
    save_connected_user,
    wait_for_authorization_code,
)

FIXTURES = Path(__file__).parent / "fixtures"
REDIRECT = "http://127.0.0.1:8888/callback"


def _load(name: str) -> dict[str, object]:
    body = json.loads((FIXTURES / name).read_text())
    assert isinstance(body, dict)
    return body


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        spotify_client_id="client-id",
        spotify_client_secret="client-secret",
        spotify_redirect_uri=REDIRECT,
        token_encryption_key=Fernet.generate_key().decode(),
        database_url="sqlite://",
    )


@pytest.fixture
def user_db():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    User.__table__.create(engine)
    settings = _settings()

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

    return engine, settings, sessions


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_authorization_code_is_exchanged_with_the_fixture():
    token = _load("spotify_token.json")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == TOKEN_URL
        seen["body"] = request.content.decode()
        seen["authorization"] = request.headers["authorization"]
        return httpx.Response(200, json=token)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = exchange_authorization_code(
            client,
            code="auth-code",
            redirect_uri=REDIRECT,
            client_id="client-id",
            client_secret="client-secret",
        )

    assert response.access_token == "access-token"
    assert response.refresh_token == "refresh-token"
    assert "grant_type=authorization_code" in seen["body"]
    assert "code=auth-code" in seen["body"]
    assert seen["authorization"].startswith("Basic ")


def test_token_rejection_is_reported_without_calling_again():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "Invalid authorization code"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AuthError, match="Invalid authorization code"):
            exchange_authorization_code(
                client,
                code="used",
                redirect_uri=REDIRECT,
                client_id="client-id",
                client_secret="client-secret",
            )


def test_profile_uses_the_access_token():
    profile = _load("spotify_me.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == PROFILE_URL
        assert request.headers["authorization"] == "Bearer access-token"
        return httpx.Response(200, json=profile)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetched = fetch_profile(client, "access-token")

    assert fetched.id == "spotify-user"
    assert fetched.display_name == "Ada"
    assert fetched.country == "US"


def test_callback_returns_the_code():
    port = _free_port()
    uri = f"http://127.0.0.1:{port}/callback"

    def on_ready() -> None:
        response = httpx.get(f"{uri}?code=abc&state=expected", timeout=2)
        assert response.status_code == 200
        assert "Signed in" in response.text

    code = wait_for_authorization_code(
        redirect_uri=uri,
        state="expected",
        timeout=5,
        on_ready=on_ready,
    )

    assert code == "abc"


def test_callback_rejects_a_mismatched_state():
    port = _free_port()
    uri = f"http://127.0.0.1:{port}/callback"

    def on_ready() -> None:
        response = httpx.get(f"{uri}?code=abc&state=other", timeout=2)
        assert response.status_code == 400

    with pytest.raises(AuthError, match="state did not match"):
        wait_for_authorization_code(redirect_uri=uri, state="expected", timeout=5, on_ready=on_ready)


def test_callback_times_out():
    port = _free_port()
    with pytest.raises(AuthError, match="Timed out"):
        wait_for_authorization_code(
            redirect_uri=f"http://127.0.0.1:{port}/callback",
            state="expected",
            timeout=0.2,
        )


def test_redirect_must_be_localhost():
    with pytest.raises(AuthError, match="127.0.0.1"):
        wait_for_authorization_code(
            redirect_uri="https://example.com/callback",
            state="expected",
            timeout=0.1,
        )


def test_sign_in_stores_encrypted_tokens(user_db, capsys):
    _engine, settings, sessions = user_db
    token = _load("spotify_token.json")
    profile = _load("spotify_me.json")
    opened: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return httpx.Response(200, json=token)
        if request.url == PROFILE_URL:
            return httpx.Response(200, json=profile)
        return httpx.Response(404)

    def receive_code(state: str) -> str:
        params = parse_qs(urlsplit(opened[0]).query)
        assert params["state"] == [state]
        assert params["client_id"] == ["client-id"]
        return "auth-code"

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run_oauth(
            settings=settings,
            http_client=client,
            session_factory=sessions,
            open_url=opened.append,
            receive_code=receive_code,
        )

    captured = capsys.readouterr()
    assert "Signed in as Ada (spotify-user)." in captured.out
    with sessions() as session:
        rows = session.scalars(select(User)).all()
        assert len(rows) == 1
        user = rows[0]
        assert user.spotify_user_id == "spotify-user"
        assert user.country == "US"
        assert decrypt_token(user.refresh_token_encrypted, settings.token_encryption_key) == "refresh-token"
        assert decrypt_token(user.access_token_encrypted or "", settings.token_encryption_key) == "access-token"
        assert user.access_token_expires_at is not None


def test_second_sign_in_replaces_the_single_user(user_db):
    engine, settings, sessions = user_db
    del engine
    with sessions() as session:
        save_connected_user(
            session,
            profile=SpotifyProfile(id="first-user", country="US", display_name="Ada"),
            refresh_token="old-refresh",
            access_token="old-access",
            expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            encryption_key=settings.token_encryption_key,
        )
        save_connected_user(
            session,
            profile=SpotifyProfile(id="second-user", country="usa", display_name=None),
            refresh_token="new-refresh",
            access_token="new-access",
            expires_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            encryption_key=settings.token_encryption_key,
        )

    with sessions() as session:
        rows = session.scalars(select(User)).all()
        assert len(rows) == 1
        assert rows[0].spotify_user_id == "second-user"
        assert rows[0].country is None
        assert (
            decrypt_token(rows[0].refresh_token_encrypted, settings.token_encryption_key) == "new-refresh"
        )


def test_refresh_persists_a_rotated_token(user_db):
    _engine, settings, sessions = user_db
    refreshed = _load("spotify_refresh.json")
    with sessions() as session:
        save_connected_user(
            session,
            profile=SpotifyProfile(id="spotify-user", country="US"),
            refresh_token="refresh-token",
            access_token="access-token",
            expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            encryption_key=settings.token_encryption_key,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        assert "grant_type=refresh_token" in request.content.decode()
        assert "refresh_token=refresh-token" in request.content.decode()
        return httpx.Response(200, json=refreshed)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        refresh_access_token(settings=settings, http_client=client, session_factory=sessions)

    with sessions() as session:
        user = session.scalar(select(User))
        assert user is not None
        key = settings.token_encryption_key
        assert decrypt_token(user.refresh_token_encrypted, key) == "new-refresh"
        assert decrypt_token(user.access_token_encrypted or "", key) == "new-access"


def test_refresh_keeps_the_old_token_when_spotify_does_not_rotate(user_db):
    _engine, settings, sessions = user_db
    with sessions() as session:
        save_connected_user(
            session,
            profile=SpotifyProfile(id="spotify-user"),
            refresh_token="refresh-token",
            access_token="access-token",
            expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            encryption_key=settings.token_encryption_key,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={"access_token": "new-access", "token_type": "Bearer", "expires_in": 3600},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        refresh_access_token(settings=settings, http_client=client, session_factory=sessions)

    with sessions() as session:
        user = session.scalar(select(User))
        assert user is not None
        assert decrypt_token(user.refresh_token_encrypted, settings.token_encryption_key) == "refresh-token"


def test_missing_credentials_stop_before_the_browser():
    opened: list[str] = []
    with pytest.raises(AuthError, match="SPOTIFY_CLIENT_ID"):
        run_oauth(
            settings=Settings(_env_file=None, spotify_client_id="", spotify_client_secret="secret"),
            open_url=opened.append,
            receive_code=lambda _state: "unused",
        )
    assert opened == []


def test_database_is_checked_before_the_browser():
    opened: list[str] = []

    @contextmanager
    def broken() -> Iterator[Session]:
        raise OperationalError("SELECT 1", {}, OSError("connection refused"))
        yield

    with pytest.raises(AuthError, match="Database unavailable"):
        run_oauth(
            settings=_settings(),
            session_factory=broken,
            open_url=opened.append,
            receive_code=lambda _state: "unused",
        )
    assert opened == []


def test_fresh_access_token_is_reused(user_db):
    _engine, settings, sessions = user_db
    with sessions() as session:
        save_connected_user(
            session,
            profile=SpotifyProfile(id="spotify-user"),
            refresh_token="refresh-token",
            access_token="still-good",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
            encryption_key=settings.token_encryption_key,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise AssertionError("a fresh access token should not be refreshed")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        token = ensure_access_token(settings=settings, http_client=client, session_factory=sessions)

    assert token == "still-good"


def test_access_token_inside_a_minute_is_refreshed(user_db):
    _engine, settings, sessions = user_db
    refreshed = _load("spotify_refresh.json")
    with sessions() as session:
        save_connected_user(
            session,
            profile=SpotifyProfile(id="spotify-user"),
            refresh_token="refresh-token",
            access_token="about-to-expire",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            encryption_key=settings.token_encryption_key,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == TOKEN_URL
        assert "grant_type=refresh_token" in request.content.decode()
        return httpx.Response(200, json=refreshed)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        token = ensure_access_token(settings=settings, http_client=client, session_factory=sessions)

    assert token == "new-access"


def test_refresh_requires_a_sign_in(user_db):
    _engine, settings, sessions = user_db
    with pytest.raises(AuthError, match="music-discovery auth"):
        refresh_access_token(settings=settings, session_factory=sessions)


def test_one_user_row_after_repeated_saves(user_db):
    _engine, settings, sessions = user_db
    with sessions() as session:
        save_connected_user(
            session,
            profile=SpotifyProfile(id="spotify-user", country="us"),
            refresh_token="first",
            access_token="first-access",
            expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            encryption_key=settings.token_encryption_key,
        )
    with sessions() as session:
        save_connected_user(
            session,
            profile=SpotifyProfile(id="spotify-user", country="US"),
            refresh_token="second",
            access_token="second-access",
            expires_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            encryption_key=settings.token_encryption_key,
        )
        assert session.scalar(select(func.count()).select_from(User)) == 1
