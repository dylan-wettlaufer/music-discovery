"""Throttled Spotify Web API client.

Retry on HTTP 429. On 401, refresh the access token once and retry. Search
goes through the resolutions cache. Do not call Spotify's recommendation,
related-artist, audio-feature, or audio-analysis endpoints.
"""

import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

API_ROOT = "https://api.spotify.com/v1"
RECENTLY_PLAYED_URL = f"{API_ROOT}/me/player/recently-played"
SAVED_TRACKS_URL = f"{API_ROOT}/me/tracks"
RECENTLY_PLAYED_LIMIT = 50
SAVED_TRACKS_PAGE_LIMIT = 50

_MAX_RATE_LIMIT_RETRIES = 3
_MAX_SAVED_PAGES = 400

TokenSource = Callable[[], str]
Sleeper = Callable[[float], None]


class SpotifyClientError(Exception):
    """A Spotify read failed after the allowed retries."""


class SpotifyAuthError(SpotifyClientError):
    """The access token was rejected. Sign in again."""


class SpotifyArtist(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    name: str = ""


class SpotifyAlbum(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    release_date: str | None = None


class SpotifyTrack(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    name: str = ""
    duration_ms: int | None = None
    popularity: int | None = None
    artists: list[SpotifyArtist] = Field(default_factory=list)
    album: SpotifyAlbum | None = None


class PlaybackContext(BaseModel):
    model_config = ConfigDict(extra="ignore")

    uri: str | None = None


class PlayedItem(BaseModel):
    """One recently-played row. ``track.id`` is null for a local file."""

    model_config = ConfigDict(extra="ignore")

    track: SpotifyTrack | None = None
    played_at: datetime
    context: PlaybackContext | None = None

    @property
    def context_uri(self) -> str | None:
        if self.context is None or not self.context.uri:
            return None
        return self.context.uri


class SavedItem(BaseModel):
    """One library row. ``track.id`` is null for a local file."""

    model_config = ConfigDict(extra="ignore")

    added_at: datetime | None = None
    track: SpotifyTrack | None = None


class _RecentlyPlayedPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[PlayedItem]


class _SavedTracksPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[SavedItem]
    next: str | None = None


class SpotifyClient:
    def __init__(
        self,
        access_token: str | TokenSource,
        *,
        http_client: httpx.Client | None = None,
        refresh: TokenSource | None = None,
        sleep: Sleeper | None = None,
    ) -> None:
        self._token: TokenSource = access_token if callable(access_token) else lambda: access_token
        self._http = http_client
        self._refresh = refresh
        self._sleep = sleep or time.sleep

    def get_me(self) -> None:
        """GET /me. Identity and market."""
        raise NotImplementedError("GET /me is not implemented.")

    def get_recently_played(self) -> list[PlayedItem]:
        """GET /me/player/recently-played. At most the last 50 qualifying plays."""
        payload = self._get_json(RECENTLY_PLAYED_URL, params={"limit": RECENTLY_PLAYED_LIMIT})
        try:
            page = _RecentlyPlayedPage.model_validate(payload)
        except ValidationError as exc:
            raise SpotifyClientError(
                "Spotify returned an unexpected recently-played response."
            ) from exc
        return page.items

    def get_top_artists(self) -> None:
        """GET /me/top/artists."""
        raise NotImplementedError("GET /me/top/artists is not implemented.")

    def get_top_tracks(self) -> None:
        """GET /me/top/tracks."""
        raise NotImplementedError("GET /me/top/tracks is not implemented.")

    def get_saved_tracks(self) -> list[SavedItem]:
        """GET /me/tracks, following ``next`` until the library is exhausted."""
        items: list[SavedItem] = []
        url: str | None = SAVED_TRACKS_URL
        params: dict[str, int] | None = {"limit": SAVED_TRACKS_PAGE_LIMIT}
        pages = 0
        while url is not None:
            pages += 1
            if pages > _MAX_SAVED_PAGES:
                raise SpotifyClientError("Saved tracks pagination did not end.")
            payload = self._get_json(url, params=params)
            try:
                page = _SavedTracksPage.model_validate(payload)
            except ValidationError as exc:
                raise SpotifyClientError(
                    "Spotify returned an unexpected saved-tracks response."
                ) from exc
            items.extend(page.items)
            url = _next_page(page.next)
            params = None
        return items

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

    def _get_json(self, url: str, *, params: dict[str, int] | None) -> dict[str, object]:
        with self._session() as http:
            return self._request_json(http, url, params)

    @contextmanager
    def _session(self) -> Iterator[httpx.Client]:
        if self._http is not None:
            yield self._http
            return
        with httpx.Client(timeout=30) as client:
            yield client

    def _request_json(
        self,
        http: httpx.Client,
        url: str,
        params: dict[str, int] | None,
    ) -> dict[str, object]:
        refreshed = False
        rate_limits = 0
        token_override: str | None = None
        while True:
            token = token_override if token_override is not None else self._token()
            response = http.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code == 401:
                if refreshed or self._refresh is None:
                    raise SpotifyAuthError(
                        "Spotify rejected the access token. "
                        "Sign in with `music-discovery auth` again."
                    )
                token_override = self._refresh()
                refreshed = True
                continue
            if response.status_code == 429:
                if rate_limits >= _MAX_RATE_LIMIT_RETRIES:
                    raise SpotifyClientError("Spotify rate limit persisted.")
                rate_limits += 1
                self._sleep(_retry_after(response))
                continue
            if response.status_code != 200:
                raise SpotifyClientError(f"Spotify request failed ({response.status_code}).")
            try:
                body = response.json()
            except ValueError as exc:
                raise SpotifyClientError("Spotify returned an unexpected response.") from exc
            if not isinstance(body, dict):
                raise SpotifyClientError("Spotify returned an unexpected response.")
            return body


def _next_page(url: str | None) -> str | None:
    if url is None:
        return None
    if not _spotify_api_url(url):
        raise SpotifyClientError("Spotify returned an unexpected saved-tracks page.")
    return url


def _spotify_api_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == "api.spotify.com"


def _retry_after(response: httpx.Response) -> float:
    raw = response.headers.get("retry-after", "1")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1.0
