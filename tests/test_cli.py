from typer.testing import CliRunner

from music_discovery.cli import app

runner = CliRunner()


def test_help_lists_the_commands():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for name in ("auth", "poll", "generate", "runs"):
        assert name in result.stdout


def test_unwired_commands_exit():
    for args in (["generate"], ["generate", "--publish"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 1
        assert "not implemented" in result.output.lower()


def test_poll_prints_the_summary(monkeypatch):
    monkeypatch.setattr(
        "music_discovery.cli.run_poll",
        lambda: "Recently played: 2 new.\nSaved tracks: 1 new.",
    )

    result = runner.invoke(app, ["poll"])

    assert result.exit_code == 0
    assert "Recently played: 2 new." in result.output
    assert "Saved tracks: 1 new." in result.output


def test_poll_reports_a_failed_sign_in(monkeypatch):
    from music_discovery.spotify.auth import AuthError

    def fail() -> str:
        raise AuthError("Sign in with `music-discovery auth` before polling.")

    monkeypatch.setattr("music_discovery.cli.run_poll", fail)

    result = runner.invoke(app, ["poll"])

    assert result.exit_code == 1
    assert "music-discovery auth" in result.output


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
