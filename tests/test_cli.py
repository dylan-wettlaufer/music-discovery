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
