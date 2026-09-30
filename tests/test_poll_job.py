import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from music_discovery.config import Settings
from music_discovery.jobs.poll_history import run_poll, run_recently_played
from music_discovery.models import JobRun, PlayEvent, User
from music_discovery.spotify.auth import TOKEN_URL, AuthError, SpotifyProfile, save_connected_user
from music_discovery.spotify.client import RECENTLY_PLAYED_URL, SAVED_TRACKS_URL, SpotifyError
from tests.history_db import history_sessions

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict[str, object]:
    body = json.loads((FIXTURES / name).read_text())
    assert isinstance(body, dict)
    return body


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        spotify_client_id="client-id",
        spotify_client_secret="client-secret",
        token_encryption_key=Fernet.generate_key().decode(),
    )


def _fresh_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=2)


def _connect(session, settings: Settings, *, expires_at: datetime) -> None:
    save_connected_user(
        session,
        profile=SpotifyProfile(id="spotify-user", country="US", display_name="Ada"),
        refresh_token="refresh-token",
        access_token="access-token",
        expires_at=expires_at,
        encryption_key=settings.token_encryption_key,
    )


def test_poll_stores_history_and_records_both_jobs():
    recent = _load("spotify_recently_played.json")
    saved = _load("spotify_saved_tracks.json")
    settings = _settings()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers["authorization"] == "Bearer access-token"
        if request.url.path.endswith("/recently-played"):
            return httpx.Response(200, json=recent)
        if request.url.path.endswith("/tracks"):
            return httpx.Response(200, json=saved)
        raise AssertionError(request.url)

    with history_sessions() as sessions, httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with sessions() as session:
            _connect(session, settings, expires_at=_fresh_expiry())
        summary = run_poll(
            settings=settings,
            session_factory=sessions,
            http_client=http,
        )
        with sessions() as session:
            jobs = session.scalars(select(JobRun).order_by(JobRun.id)).all()
            job_rows = [(job.job_name, job.status, job.gap_warning, job.details) for job in jobs]
            plays = session.scalar(select(func.count()).select_from(PlayEvent))
            user = session.scalar(select(User))
            cursor = user.recently_played_cursor if user is not None else None

    assert summary == "Recently played: 1 new.\nSaved tracks: 1 new."
    assert calls == [RECENTLY_PLAYED_URL + "?limit=50", SAVED_TRACKS_URL + "?limit=50"]
    assert job_rows == [
        ("recently_played", "succeeded", False, {"fetched": 3, "inserted": 1, "skipped": 2}),
        ("saved_tracks", "succeeded", False, {"fetched": 2, "inserted": 1, "skipped": 1}),
    ]
    assert plays == 1
    assert cursor is not None
    if cursor.tzinfo is None:
        cursor = cursor.replace(tzinfo=timezone.utc)
    assert cursor == datetime(2026, 9, 29, 18, 4, 12, 123000, tzinfo=timezone.utc)


def test_expiring_token_is_refreshed_before_the_poll():
    recent = _load("spotify_recently_played.json")
    saved = _load("spotify_saved_tracks.json")
    refreshed = _load("spotify_refresh.json")
    settings = _settings()
    tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return httpx.Response(200, json=refreshed)
        tokens.append(request.headers["authorization"])
        if request.url.path.endswith("/recently-played"):
            return httpx.Response(200, json=recent)
        return httpx.Response(200, json=saved)

    with history_sessions() as sessions, httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with sessions() as session:
            _connect(session, settings, expires_at=datetime.now(timezone.utc) + timedelta(seconds=30))
        run_poll(settings=settings, session_factory=sessions, http_client=http)

    assert tokens
    assert set(tokens) == {"Bearer new-access"}


def test_unauthorized_read_refreshes_once():
    recent = _load("spotify_recently_played.json")
    refreshed = _load("spotify_refresh.json")
    settings = _settings()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url == TOKEN_URL:
            return httpx.Response(200, json=refreshed)
        attempts += 1
        if request.headers["authorization"] == "Bearer access-token":
            return httpx.Response(401, json={"error": {"message": "The access token expired"}})
        return httpx.Response(200, json=recent)

    with history_sessions() as sessions, httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with sessions() as session:
            _connect(session, settings, expires_at=_fresh_expiry())
        stats = run_recently_played(settings=settings, session_factory=sessions, http_client=http)
        with sessions() as session:
            job = session.scalar(select(JobRun))

    assert attempts == 2
    assert stats.inserted == 1
    assert job is not None
    assert job.status == "succeeded"


def test_revoked_token_stops_before_saved_tracks():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == TOKEN_URL
        return httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "Refresh token revoked"},
        )

    with history_sessions() as sessions, httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with sessions() as session:
            _connect(session, settings, expires_at=datetime.now(timezone.utc) - timedelta(minutes=5))
        with pytest.raises(AuthError, match="music-discovery auth"):
            run_poll(settings=settings, session_factory=sessions, http_client=http)
        with sessions() as session:
            jobs = session.scalars(select(JobRun)).all()
            recorded = [(job.job_name, job.status, job.error) for job in jobs]
            plays = session.scalar(select(func.count()).select_from(PlayEvent))

    assert recorded == [("recently_played", "failed", recorded[0][2])]
    assert recorded[0][2] is not None
    assert "Refresh token revoked" in recorded[0][2]
    assert plays == 0


def test_missing_user_is_recorded_as_a_failed_poll():
    settings = _settings()
    with history_sessions() as sessions:
        with pytest.raises(AuthError, match="before polling"):
            run_recently_played(settings=settings, session_factory=sessions)
        with sessions() as session:
            job = session.scalar(select(JobRun))
            status = job.status if job is not None else None
            name = job.job_name if job is not None else None

    assert name == "recently_played"
    assert status == "failed"


def test_overflowed_window_sets_the_gap_warning():
    source = _load("spotify_recently_played.json")["items"]
    assert isinstance(source, list)
    first = source[0]
    start = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    items = []
    for index in range(50):
        item = json.loads(json.dumps(first))
        played = start + timedelta(minutes=index)
        item["played_at"] = played.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        items.append(item)
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"items": items, "limit": 50, "next": None})

    with history_sessions() as sessions, httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with sessions() as session:
            _connect(session, settings, expires_at=_fresh_expiry())
            user = session.scalar(select(User))
            assert user is not None
            user.recently_played_cursor = start - timedelta(hours=1)
        stats = run_recently_played(settings=settings, session_factory=sessions, http_client=http)
        with sessions() as session:
            job = session.scalar(select(JobRun))
            gap = job.gap_warning if job is not None else None

    assert stats.gap_warning is True
    assert stats.inserted == 50
    assert gap is True


def test_spotify_error_fails_the_job():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={"error": {"message": "unavailable"}})

    with history_sessions() as sessions, httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with sessions() as session:
            _connect(session, settings, expires_at=_fresh_expiry())
        with pytest.raises(SpotifyError, match="unavailable"):
            run_recently_played(settings=settings, session_factory=sessions, http_client=http)
        with sessions() as session:
            job = session.scalar(select(JobRun))
            status = job.status if job is not None else None
            error = job.error if job is not None else None

    assert status == "failed"
    assert error is not None
    assert "unavailable" in error
