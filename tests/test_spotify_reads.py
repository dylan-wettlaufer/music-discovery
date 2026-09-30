import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from music_discovery.spotify.client import (
    RECENTLY_PLAYED_URL,
    SAVED_TRACKS_URL,
    SpotifyClient,
    SpotifyError,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict[str, object]:
    body = json.loads((FIXTURES / name).read_text())
    assert isinstance(body, dict)
    return body


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    refresh: Callable[[], str] | None = None,
    sleep: Callable[[float], None] | None = None,
    token: str = "access-token",
) -> tuple[httpx.Client, SpotifyClient]:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return http, SpotifyClient(token, http_client=http, refresh=refresh, sleep=sleep)


def test_recently_played_reads_the_full_window_without_a_cursor():
    payload = _load("spotify_recently_played.json")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer access-token"
        seen["url"] = str(request.url)
        assert request.url.params["limit"] == "50"
        assert "after" not in request.url.params
        assert "before" not in request.url.params
        return httpx.Response(200, json=payload)

    http, client = _client(handler)
    with http:
        page = client.get_recently_played()

    assert seen["url"].startswith(RECENTLY_PLAYED_URL)
    assert page.item_count == 3
    assert page.skipped_local == 2
    assert len(page.items) == 3
    play = page.items[0]
    assert play.track is not None
    assert play.track.id == "6ol4ZSifr7r3JnY7F5y6kE"
    assert play.track.name == "Karma Police"
    assert play.track.primary_artist == "Radiohead"
    assert play.track.artist_ids == ["4Z8W4fKeB5YxbusRsdQVPb"]
    assert play.track.album_name == "OK Computer"
    assert play.track.release_date == "1997-06-16"
    assert play.track.duration_ms == 261866
    assert play.track.popularity == 78
    assert play.context_uri == "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"
    assert play.played_at.isoformat() == "2026-09-29T18:04:12.123000+00:00"
    assert page.items[1].track is None
    assert page.items[2].track is None


def test_recently_played_accepts_a_fifty_item_window():
    item = _load("spotify_recently_played.json")["items"]
    assert isinstance(item, list)
    first = item[0]
    payload = {"items": [first for _ in range(50)], "limit": 50, "next": None}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["limit"] == "50"
        return httpx.Response(200, json=payload)

    http, client = _client(handler)
    with http:
        page = client.get_recently_played()

    assert page.item_count == 50
    assert page.skipped_local == 0
    assert all(play.track is not None for play in page.items)


def test_saved_tracks_follow_next_until_the_last_page():
    first = _load("spotify_saved_tracks.json")
    second_track = {
        "added_at": "2019-05-05T08:00:00Z",
        "track": {
            "id": "0bYg9bo50gSsH3LtXe2SQn",
            "name": "All I Need",
            "artists": [{"id": "4Z8W4fKeB5YxbusRsdQVPb", "name": "Radiohead"}],
            "album": {"name": "In Rainbows", "release_date": "2007-10-10"},
            "duration_ms": 228000,
            "popularity": 70,
        },
    }
    next_url = f"{SAVED_TRACKS_URL}?offset=50&limit=50"
    first = {**first, "next": next_url, "total": 3}
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.params.get("offset") == "50":
            assert "limit" not in request.url.params or request.url.params["limit"] == "50"
            return httpx.Response(200, json={"items": [second_track], "next": None, "limit": 50})
        assert request.url.params["limit"] == "50"
        assert "offset" not in request.url.params
        return httpx.Response(200, json=first)

    http, client = _client(handler)
    with http:
        saved = client.get_saved_tracks()

    assert calls[0].startswith(SAVED_TRACKS_URL)
    assert len(calls) == 2
    assert saved.fetched == 3
    assert saved.skipped_local == 1
    tracks = [item.track for item in saved.items if item.track is not None]
    assert [track.id for track in tracks] == [
        "3AJwUDP919kvQ9QcozQPxg",
        "0bYg9bo50gSsH3LtXe2SQn",
    ]
    assert saved.items[0].added_at is not None
    assert saved.items[0].added_at.isoformat() == "2024-03-01T12:00:00+00:00"


def test_unauthorized_refreshes_once_and_retries():
    payload = _load("spotify_recently_played.json")
    tokens: list[str] = []
    refreshes = 0

    def refresh() -> str:
        nonlocal refreshes
        refreshes += 1
        return "refreshed-token"

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer access-token":
            return httpx.Response(401, json={"error": {"status": 401, "message": "The access token expired"}})
        return httpx.Response(200, json=payload)

    http, client = _client(handler, refresh=refresh)
    with http:
        page = client.get_recently_played()

    assert refreshes == 1
    assert tokens == ["Bearer access-token", "Bearer refreshed-token"]
    assert page.item_count == 3


def test_a_second_unauthorized_response_stops():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(401, json={"error": {"status": 401, "message": "Invalid access token"}})

    http, client = _client(handler, refresh=lambda: "still-bad")
    with http, pytest.raises(SpotifyError, match="music-discovery auth"):
        client.get_recently_played()


def test_rate_limit_sleeps_for_retry_after_then_reads():
    payload = _load("spotify_saved_tracks.json")
    slept: list[float] = []
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        del request
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json=payload)

    http, client = _client(handler, sleep=slept.append)
    with http:
        saved = client.get_saved_tracks()

    assert slept == [2.0]
    assert saved.fetched == 2
