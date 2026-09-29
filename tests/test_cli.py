from typer.testing import CliRunner

from music_discovery.cli import app

runner = CliRunner()


def test_help_lists_the_commands():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for name in ("auth", "poll", "generate", "runs"):
        assert name in result.stdout


def test_unwired_commands_exit():
    for args in (["auth"], ["poll"], ["generate"], ["generate", "--publish"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 1
        assert "not implemented" in result.output.lower()
