import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from music_discovery.spotify.client import (
    ME_URL,
    RECENTLY_PLAYED_URL,
    SAVED_TRACKS_URL,
    SEARCH_URL,
    SpotifyAuthError,
    SpotifyClient,
    SpotifyClientError,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict[str, object]:
    body = json.loads((FIXTURES / name).read_text())
    assert isinstance(body, dict)
    return body


def _client(handler, **kwargs) -> tuple[httpx.Client, SpotifyClient]:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return http, SpotifyClient("access-token", http_client=http, **kwargs)


def test_get_me_reads_the_current_user():
    payload = _load("spotify_me.json")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(ME_URL)
        assert str(request.url) == "https://api.spotify.com/v1/me"
        seen["authorization"] = request.headers["authorization"]
        return httpx.Response(200, json=payload)

    http, client = _client(handler)
    with http:
        user = client.get_me()

    assert seen["authorization"] == "Bearer access-token"
    assert user.id == "spotify-user"
    assert user.display_name == "Ada"
    assert user.country == "US"
    assert user.email == "ada@example.com"
    assert user.product == "premium"
    assert user.follower_count == 12
    assert user.profile_url == "https://open.spotify.com/user/spotify-user"
    assert user.uri == "spotify:user:spotify-user"
    assert user.explicit_content is not None
    assert user.explicit_content.filter_enabled is False
    assert user.explicit_content.filter_locked is False


def test_get_me_rejects_an_unexpected_profile():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"display_name": "Ada"})

    http, client = _client(handler)
    with http:
        with pytest.raises(SpotifyClientError, match="unexpected profile"):
            client.get_me()


def test_recently_played_reads_the_fixture_and_keeps_a_null_track_id():
    payload = _load("spotify_recently_played.json")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.copy_with(query=None) == httpx.URL(RECENTLY_PLAYED_URL)
        assert request.url.params["limit"] == "50"
        assert "after" not in request.url.params
        assert "before" not in request.url.params
        seen["authorization"] = request.headers["authorization"]
        return httpx.Response(200, json=payload)

    http, client = _client(handler)
    with http:
        items = client.get_recently_played()

    assert seen["authorization"] == "Bearer access-token"
    assert len(items) == 2
    assert items[0].track is not None
    assert items[0].track.id == "track-karma"
    assert items[0].track.name == "Karma Police"
    assert items[0].track.artists[0].name == "Radiohead"
    assert items[0].track.album is not None
    assert items[0].track.album.name == "OK Computer"
    assert items[0].played_at == datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc)
    assert items[0].context_uri == "spotify:playlist:fresh"
    assert items[1].track is not None
    assert items[1].track.id is None


def test_recently_played_returns_a_full_window():
    payload = _load("spotify_recently_played.json")
    items = payload["items"]
    assert isinstance(items, list)
    template = items[0]
    assert isinstance(template, dict)
    window = []
    for index in range(50):
        item = json.loads(json.dumps(template))
        item["track"]["id"] = f"track-{index}"
        window.append(item)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["limit"] == "50"
        assert "after" not in request.url.params
        return httpx.Response(200, json={"items": window})

    http, client = _client(handler)
    with http:
        played = client.get_recently_played()

    assert len(played) == 50
    assert played[0].track is not None and played[0].track.id == "track-0"
    assert played[49].track is not None and played[49].track.id == "track-49"


def test_saved_tracks_follow_next_until_it_is_absent():
    page1 = _load("spotify_saved_tracks_page1.json")
    page2 = _load("spotify_saved_tracks_page2.json")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.params.get("offset") == "50":
            return httpx.Response(200, json=page2)
        assert request.url.copy_with(query=None) == httpx.URL(SAVED_TRACKS_URL)
        assert request.url.params["limit"] == "50"
        return httpx.Response(200, json=page1)

    http, client = _client(handler)
    with http:
        saved = client.get_saved_tracks()

    assert len(requested) == 2
    assert saved[0].track is not None and saved[0].track.id == "track-karma"
    assert saved[0].added_at == datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert saved[1].track is not None and saved[1].track.id == "track-weird-fishes"


def test_unauthorized_refreshes_once_and_retries():
    payload = _load("spotify_recently_played.json")
    tokens: list[str] = []
    refreshed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers["authorization"])
        if len(tokens) == 1:
            return httpx.Response(401, json={"error": {"status": 401, "message": "Invalid access token"}})
        return httpx.Response(200, json=payload)

    def refresh() -> str:
        refreshed.append("refreshed")
        return "new-token"

    http, client = _client(handler, refresh=refresh)
    with http:
        items = client.get_recently_played()

    assert tokens == ["Bearer access-token", "Bearer new-token"]
    assert refreshed == ["refreshed"]
    assert len(items) == 2


def test_a_second_unauthorized_response_stops():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        del request
        calls += 1
        return httpx.Response(401, json={"error": {"status": 401, "message": "Invalid access token"}})

    def refresh() -> str:
        return "new-token"

    http, client = _client(handler, refresh=refresh)
    with http:
        with pytest.raises(SpotifyAuthError, match="music-discovery auth"):
            client.get_recently_played()

    assert calls == 2


def test_rate_limit_sleeps_and_retries():
    payload = _load("spotify_recently_played.json")
    slept: list[float] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        del request
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, json={"error": "rate limit"})
        return httpx.Response(200, json=payload)

    http, client = _client(handler, sleep=slept.append)
    with http:
        items = client.get_recently_played()

    assert slept == [2.0]
    assert calls == 2
    assert items[0].track is not None
    assert items[0].track.id == "track-karma"


def test_a_persistent_rate_limit_stops():
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(429, headers={"retry-after": "1"}, json={"error": "rate limit"})

    http, client = _client(handler, sleep=slept.append)
    with http:
        with pytest.raises(SpotifyClientError, match="rate limit"):
            client.get_recently_played()

    assert slept == [1.0, 1.0, 1.0]


def test_top_artists_and_tracks_send_the_time_range():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.path}?{request.url.params['time_range']}")
        if request.url.path.endswith("/artists"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "artist-radiohead",
                            "name": "Radiohead",
                            "genres": ["art rock"],
                            "popularity": 80,
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "track-karma",
                        "name": "Karma Police",
                        "artists": [{"id": "artist-radiohead", "name": "Radiohead"}],
                    }
                ]
            },
        )

    http, client = _client(handler)
    with http:
        artists = client.get_top_artists(time_range="long_term")
        tracks = client.get_top_tracks(time_range="medium_term")

    assert seen == ["/v1/me/top/artists?long_term", "/v1/me/top/tracks?medium_term"]
    assert artists[0].genres == ["art rock"]
    assert tracks[0].id == "track-karma"


def test_artist_albums_skip_compilations_and_album_tracks_page():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/albums"):
            assert request.url.params["include_groups"] == "album"
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "album-bends",
                            "name": "The Bends",
                            "album_type": "album",
                            "release_date": "1995-03-13",
                        }
                    ],
                    "next": None,
                },
            )
        return httpx.Response(
            200,
            json={"items": [{"id": "track-deep", "name": "Planet Telex"}], "next": None},
        )

    http, client = _client(handler)
    with http:
        albums = client.get_artist_albums("artist-radiohead")
        assert albums[0].album_type == "album"
        tracks = client.get_album_tracks("album-bends")

    assert tracks[0].id == "track-deep"


def test_tracks_and_artists_are_fetched_one_at_a_time_and_market_search():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/tracks/"):
            track_id = request.url.path.rsplit("/", 1)[-1]
            if track_id == "track-b":
                return httpx.Response(404, json={"error": {"status": 404}})
            assert track_id == "track-a"
            return httpx.Response(200, json={"id": "track-a", "name": "A", "popularity": 10})
        if request.url.path.startswith("/v1/artists/"):
            assert request.url.path == "/v1/artists/artist-portishead"
            return httpx.Response(
                200,
                json={"id": "artist-portishead", "name": "Portishead", "genres": ["trip hop"]},
            )
        assert request.url.copy_with(query=None) == httpx.URL(SEARCH_URL)
        assert request.url.params["type"] == "track"
        assert request.url.params["market"] == "US"
        return httpx.Response(
            200,
            json={
                "tracks": {
                    "items": [
                        {
                            "id": "track-glory",
                            "name": "Glory Box",
                            "artists": [{"name": "Portishead"}],
                            "album": {"album_type": "album", "name": "Dummy"},
                        }
                    ]
                }
            },
        )

    http, client = _client(handler)
    with http:
        tracks = client.get_tracks(["track-a", "track-b"])
        artists = client.get_artists(["artist-portishead"])
        found = client.search_tracks('track:"Glory Box" artist:"Portishead"', market="US")

    assert [track.id for track in tracks] == ["track-a"]
    assert artists[0].genres == ["trip hop"]
    assert found[0].album is not None
    assert found[0].album.album_type == "album"


def test_create_playlist_is_private_and_can_be_found_by_name():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["public"] is False
            assert body["name"] == "Fresh — Sep 27"
            assert request.url.path == "/v1/me/playlists"
            return httpx.Response(201, json={"id": "playlist-1", "name": body["name"]})
        if request.method == "PUT":
            body = json.loads(request.content)
            assert request.url.path == "/v1/playlists/playlist-1/items"
            assert body["uris"] == ["spotify:track:track-glory"]
            return httpx.Response(200, json={"snapshot_id": "snap"})
        if request.method == "POST":
            return httpx.Response(500)
        return httpx.Response(
            200,
            json={"items": [{"id": "playlist-1", "name": "Fresh — Sep 27"}], "next": None},
        )

    http, client = _client(handler)
    with http:
        created = client.create_playlist("spotify-user", "Fresh — Sep 27", description="Tracks you have not played.")
        client.replace_playlist_tracks(created, ["spotify:track:track-glory"])
        found = client.find_playlist_id("Fresh — Sep 27")

    assert created == "playlist-1"
    assert found == "playlist-1"
