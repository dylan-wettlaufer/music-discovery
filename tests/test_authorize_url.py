from urllib.parse import parse_qs, urlsplit

from music_discovery.spotify.auth import SCOPES, build_authorize_url


def test_authorize_url_requests_the_locked_scopes():
    url = build_authorize_url(
        client_id="client",
        redirect_uri="http://127.0.0.1:8888/callback",
        state="state-token",
    )
    parts = urlsplit(url)
    params = parse_qs(parts.query)

    assert parts.scheme == "https"
    assert parts.netloc == "accounts.spotify.com"
    assert parts.path == "/authorize"
    assert params["response_type"] == ["code"]
    assert params["client_id"] == ["client"]
    assert params["redirect_uri"] == ["http://127.0.0.1:8888/callback"]
    assert params["state"] == ["state-token"]
    assert set(params["scope"][0].split()) == set(SCOPES)
