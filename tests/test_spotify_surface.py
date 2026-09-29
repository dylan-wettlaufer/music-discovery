from pathlib import Path

_REMOVED = (
    "/recommendations",
    "/audio-features",
    "/audio-analysis",
    "/related-artists",
)


def test_spotify_client_does_not_call_removed_endpoints():
    root = Path(__file__).resolve().parents[1] / "src" / "music_discovery" / "spotify"
    text = "\n".join(path.read_text() for path in root.rglob("*.py"))
    for endpoint in _REMOVED:
        assert endpoint not in text
