"""Throttled Spotify Web API client.

Retry on HTTP 429. On 401, refresh the access token once and retry. Search
goes through the resolutions cache. Do not call Spotify's recommendation,
related-artist, audio-feature, or audio-analysis endpoints.
"""

import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

API_ROOT = "https://api.spotify.com/v1"
RECENTLY_PLAYED_URL = f"{API_ROOT}/me/player/recently-played"
SAVED_TRACKS_URL = f"{API_ROOT}/me/tracks"
RECENTLY_PLAYED_LIMIT = 50
SAVED_TRACKS_LIMIT = 50

_MAX_RATE_LIMIT_RETRIES = 3


class SpotifyError(Exception):
    """A Spotify read failed. The message is safe to show."""


class SpotifyTrack(BaseModel):
    """Fields the history poll stores. Other Spotify fields are ignored."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    primary_artist: str
    artist_ids: list[str]
    album_name: str | None = None
    duration_ms: int | None = None
    release_date: str | None = None
    popularity: int | None = None


class PlayedItem(BaseModel):
    """One recently-played row. ``track`` is absent for a local file."""

    model_config = ConfigDict(extra="ignore")

    track: SpotifyTrack | None
    played_at: datetime
    context_uri: str | None = None


class RecentlyPlayed(BaseModel):
    """The current 50-play window, including local files that cannot be stored."""

    model_config = ConfigDict(extra="ignore")

    item_count: int
    items: list[PlayedItem]
    skipped_local: int


class SavedItem(BaseModel):
    """One library row. ``track`` is absent for a local file."""

    model_config = ConfigDict(extra="ignore")

    track: SpotifyTrack | None
    added_at: datetime | None = None


class SavedTracks(BaseModel):
    """Every page of ``GET /me/tracks``."""

    model_config = ConfigDict(extra="ignore")

    items: list[SavedItem]
    skipped_local: int
    fetched: int


class SpotifyClient:
    def __init__(
        self,
        access_token: str,
        *,
        http_client: httpx.Client | None = None,
        refresh: Callable[[], str] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._access_token = access_token
        self._refresh = refresh
        self._sleep = sleep or time.sleep
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=30)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "SpotifyClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_me(self) -> None:
        """GET /me. Identity and market."""
        raise NotImplementedError("GET /me is not implemented.")

    def get_recently_played(self) -> RecentlyPlayed:
        """GET /me/player/recently-played. At most the last 50 qualifying plays."""
        payload = self._get_json(RECENTLY_PLAYED_URL, params={"limit": RECENTLY_PLAYED_LIMIT})
        return parse_recently_played(payload)

    def get_top_artists(self) -> None:
        """GET /me/top/artists."""
        raise NotImplementedError("GET /me/top/artists is not implemented.")

    def get_top_tracks(self) -> None:
        """GET /me/top/tracks."""
        raise NotImplementedError("GET /me/top/tracks is not implemented.")

    def get_saved_tracks(self) -> SavedTracks:
        """GET /me/tracks, following ``next`` until Spotify has no further page."""
        url: str | None = SAVED_TRACKS_URL
        params: dict[str, int] | None = {"limit": SAVED_TRACKS_LIMIT}
        items: list[SavedItem] = []
        skipped_local = 0
        fetched = 0
        seen: set[str] = set()
        while url is not None:
            if url in seen:
                raise SpotifyError("Spotify saved-tracks paging repeated a page.")
            seen.add(url)
            payload = self._get_json(url, params=params)
            params = None
            page = _parse_saved_page(payload)
            items.extend(page)
            fetched += _raw_count(payload)
            skipped_local += sum(1 for item in page if item.track is None)
            nxt = payload.get("next")
            url = nxt if isinstance(nxt, str) and nxt else None
        return SavedTracks(items=items, skipped_local=skipped_local, fetched=fetched)

    def get_artist_albums(self) -> None:
        """GET /artists/{id}/albums. Albums only; skip compilations."""
        raise NotImplementedError("GET /artists/{id}/albums is not implemented.")

    def get_album_tracks(self) -> None:
        """GET /albums/{id}/tracks."""
        raise NotImplementedError("GET /albums/{id}/tracks is not implemented.")

    def get_tracks(self) -> None:
        """Batched GET /tracks."""
        raise NotImplementedError("GET /tracks is not implemented.")

    def get_artists(self) -> None:
        """Batched GET /artists. Genres still live on the artist object."""
        raise NotImplementedError("GET /artists is not implemented.")

    def search_tracks(self) -> None:
        """GET /search?type=track, with the user's market."""
        raise NotImplementedError("GET /search is not implemented.")

    def create_playlist(self) -> None:
        """POST /users/{id}/playlists. Private."""
        raise NotImplementedError("POST /users/{id}/playlists is not implemented.")

    def add_playlist_tracks(self) -> None:
        """POST /playlists/{id}/tracks."""
        raise NotImplementedError("POST /playlists/{id}/tracks is not implemented.")

    def _get_json(self, url: str, *, params: dict[str, int] | None) -> dict[str, Any]:
        refreshed = False
        rate_retries = 0
        while True:
            response = self._http.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self._access_token}"},
            )
            if response.status_code == 401:
                if refreshed or self._refresh is None:
                    raise SpotifyError("Sign in with `music-discovery auth` again.")
                self._access_token = self._refresh()
                refreshed = True
                continue
            if response.status_code == 429:
                if rate_retries >= _MAX_RATE_LIMIT_RETRIES:
                    raise SpotifyError("Spotify rate limit persisted.")
                rate_retries += 1
                self._sleep(_retry_after_seconds(response))
                continue
            if response.status_code != 200:
                raise SpotifyError(
                    f"Spotify request failed ({response.status_code}): {_spotify_error(response)}"
                )
            return _json_object(response)


def parse_recently_played(payload: object) -> RecentlyPlayed:
    raw_items = _items(payload, "recently-played")
    items: list[PlayedItem] = []
    skipped_local = 0
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        played_at = _parse_time(raw.get("played_at"))
        if played_at is None:
            continue
        track = parse_spotify_track(raw.get("track"))
        if track is None:
            skipped_local += 1
        items.append(
            PlayedItem(
                track=track,
                played_at=played_at,
                context_uri=_context_uri(raw.get("context")),
            )
        )
    return RecentlyPlayed(item_count=len(raw_items), items=items, skipped_local=skipped_local)


def parse_spotify_track(raw: object) -> SpotifyTrack | None:
    """Return a track with a Spotify id. Local files and empty objects are dropped."""
    if not isinstance(raw, dict):
        return None
    track_id = raw.get("id")
    if not isinstance(track_id, str) or not track_id:
        return None
    name = raw.get("name")
    artists = raw.get("artists")
    primary_artist = ""
    artist_ids: list[str] = []
    if isinstance(artists, list):
        for index, artist in enumerate(artists):
            if not isinstance(artist, dict):
                continue
            artist_name = artist.get("name")
            if index == 0 and isinstance(artist_name, str):
                primary_artist = artist_name
            artist_id = artist.get("id")
            if isinstance(artist_id, str) and artist_id:
                artist_ids.append(artist_id)
    album = raw.get("album")
    album_name = None
    release_date = None
    if isinstance(album, dict):
        if isinstance(album.get("name"), str):
            album_name = album["name"]
        if isinstance(album.get("release_date"), str):
            release_date = album["release_date"]
    duration_ms = raw.get("duration_ms")
    popularity = raw.get("popularity")
    return SpotifyTrack(
        id=track_id,
        name=name if isinstance(name, str) else "",
        primary_artist=primary_artist,
        artist_ids=artist_ids,
        album_name=album_name,
        duration_ms=duration_ms if isinstance(duration_ms, int) else None,
        release_date=release_date,
        popularity=popularity if isinstance(popularity, int) else None,
    )


def _parse_saved_page(payload: object) -> list[SavedItem]:
    page: list[SavedItem] = []
    for raw in _items(payload, "saved tracks"):
        if not isinstance(raw, dict):
            continue
        page.append(
            SavedItem(
                track=parse_spotify_track(raw.get("track")),
                added_at=_parse_time(raw.get("added_at")),
            )
        )
    return page


def _items(payload: object, label: str) -> list[object]:
    if not isinstance(payload, dict):
        raise SpotifyError(f"Spotify returned an unexpected {label} response.")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise SpotifyError(f"Spotify returned an unexpected {label} response.")
    return raw_items


def _raw_count(payload: dict[str, Any]) -> int:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return 0
    return len(raw_items)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _context_uri(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    uri = value.get("uri")
    if isinstance(uri, str) and uri:
        return uri
    return None


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise SpotifyError("Spotify returned an unexpected response.") from exc
    if not isinstance(body, dict):
        raise SpotifyError("Spotify returned an unexpected response.")
    return body


def _retry_after_seconds(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After", "1").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return 1.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _spotify_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "request rejected"
    if not isinstance(body, dict):
        return "request rejected"
    error = body.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    if isinstance(error, str) and error:
        return error
    return "request rejected"
