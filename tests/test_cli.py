import json
from pathlib import Path

import httpx
from typer.testing import CliRunner

from music_discovery.cli import app, load_profile
from music_discovery.spotify.auth import AuthError
from music_discovery.spotify.client import SpotifyClientError, SpotifyUser

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


def test_help_lists_the_commands():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for name in ("auth", "poll", "generate", "runs", "profile"):
        assert name in result.stdout


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
