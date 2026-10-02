"""Throttled Spotify Web API client.

Retry on HTTP 429. On 401, refresh the access token once and retry. Search
goes through the resolutions cache. Do not call Spotify's recommendation,
related-artist, audio-feature, or audio-analysis endpoints.
"""

import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

API_ROOT = "https://api.spotify.com/v1"
ME_URL = f"{API_ROOT}/me"
RECENTLY_PLAYED_URL = f"{API_ROOT}/me/player/recently-played"
SAVED_TRACKS_URL = f"{API_ROOT}/me/tracks"
TOP_ARTISTS_URL = f"{API_ROOT}/me/top/artists"
TOP_TRACKS_URL = f"{API_ROOT}/me/top/tracks"
SEARCH_URL = f"{API_ROOT}/search"
PLAYLISTS_URL = f"{API_ROOT}/me/playlists"
RECENTLY_PLAYED_LIMIT = 50
SAVED_TRACKS_PAGE_LIMIT = 50
TRACK_BATCH_SIZE = 50

_MAX_RATE_LIMIT_RETRIES = 3
_MAX_SAVED_PAGES = 400
_MAX_PAGES = 20
_TIME_RANGES = frozenset({"short_term", "medium_term", "long_term"})
_Query = Mapping[str, str | int]

TokenSource = Callable[[], str]
Sleeper = Callable[[float], None]


class SpotifyClientError(Exception):
    """A Spotify read failed after the allowed retries."""


class SpotifyAuthError(SpotifyClientError):
    """The access token was rejected. Sign in again."""


class _ExplicitContent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    filter_enabled: bool = False
    filter_locked: bool = False


class _Followers(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total: int = 0


class _ExternalUrls(BaseModel):
    model_config = ConfigDict(extra="ignore")

    spotify: str | None = None


class SpotifyUser(BaseModel):
    """Current user from ``GET /me``."""

    model_config = ConfigDict(extra="ignore")

    id: str
    display_name: str | None = None
    country: str | None = None
    email: str | None = None
    product: str | None = None
    uri: str | None = None
    followers: _Followers | None = None
    external_urls: _ExternalUrls | None = None
    explicit_content: _ExplicitContent | None = None

    @property
    def follower_count(self) -> int | None:
        if self.followers is None:
            return None
        return self.followers.total

    @property
    def profile_url(self) -> str | None:
        if self.external_urls is None or not self.external_urls.spotify:
            return None
        return self.external_urls.spotify


class SpotifyArtist(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    name: str = ""
    genres: list[str] = Field(default_factory=list)
    popularity: int | None = None


class SpotifyAlbum(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    name: str | None = None
    release_date: str | None = None
    album_type: str | None = None


class SpotifyTrack(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    uri: str | None = None
    name: str = ""
    duration_ms: int | None = None
    popularity: int | None = None
    artists: list[SpotifyArtist] = Field(default_factory=list)
    album: SpotifyAlbum | None = None


class SpotifyPlaylist(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str = ""


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


class _ArtistPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[SpotifyArtist] = Field(default_factory=list)
    next: str | None = None


class _TrackPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[SpotifyTrack] = Field(default_factory=list)
    next: str | None = None


class _AlbumPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[SpotifyAlbum] = Field(default_factory=list)
    next: str | None = None


class _PlaylistPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[SpotifyPlaylist] = Field(default_factory=list)
    next: str | None = None


class _SearchTracks(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[SpotifyTrack] = Field(default_factory=list)


class _SearchPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tracks: _SearchTracks | None = None


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

    def get_me(self) -> SpotifyUser:
        """GET /me. Identity, market, and the public profile."""
        payload = self._get_json(ME_URL, params=None)
        try:
            return SpotifyUser.model_validate(payload)
        except ValidationError as exc:
            raise SpotifyClientError("Spotify returned an unexpected profile.") from exc

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

    def get_top_artists(self, *, time_range: str = "medium_term", limit: int = 50) -> list[SpotifyArtist]:
        """GET /me/top/artists."""
        _require_time_range(time_range)
        payload = self._get_json(
            TOP_ARTISTS_URL,
            params={"time_range": time_range, "limit": limit},
        )
        try:
            return _ArtistPage.model_validate(payload).items
        except ValidationError as exc:
            raise SpotifyClientError("Spotify returned an unexpected top-artists response.") from exc

    def get_top_tracks(self, *, time_range: str = "medium_term", limit: int = 50) -> list[SpotifyTrack]:
        """GET /me/top/tracks."""
        _require_time_range(time_range)
        payload = self._get_json(
            TOP_TRACKS_URL,
            params={"time_range": time_range, "limit": limit},
        )
        try:
            return _TrackPage.model_validate(payload).items
        except ValidationError as exc:
            raise SpotifyClientError("Spotify returned an unexpected top-tracks response.") from exc

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

    def get_artist_albums(self, artist_id: str) -> list[SpotifyAlbum]:
        """GET /artists/{id}/albums. Albums only; skip compilations."""
        pages = self._collect_pages(
            f"{API_ROOT}/artists/{artist_id}/albums",
            params={"include_groups": "album", "limit": 50},
            model=_AlbumPage,
            failure="Spotify returned an unexpected albums response.",
        )
        return [item for item in pages if isinstance(item, SpotifyAlbum)]

    def get_album_tracks(self, album_id: str) -> list[SpotifyTrack]:
        """GET /albums/{id}/tracks."""
        pages = self._collect_pages(
            f"{API_ROOT}/albums/{album_id}/tracks",
            params={"limit": 50},
            model=_TrackPage,
            failure="Spotify returned an unexpected album-tracks response.",
        )
        return [item for item in pages if isinstance(item, SpotifyTrack)]

    def get_tracks(self, track_ids: Sequence[str]) -> list[SpotifyTrack]:
        """Batched GET /tracks."""
        found: list[SpotifyTrack] = []
        for chunk in _chunks(track_ids, TRACK_BATCH_SIZE):
            payload = self._get_json(f"{API_ROOT}/tracks", params={"ids": ",".join(chunk)})
            rows = payload.get("tracks")
            if not isinstance(rows, list):
                raise SpotifyClientError("Spotify returned an unexpected tracks response.")
            for row in rows:
                if not row:
                    continue
                try:
                    found.append(SpotifyTrack.model_validate(row))
                except ValidationError as exc:
                    raise SpotifyClientError(
                        "Spotify returned an unexpected tracks response."
                    ) from exc
        return found

    def get_artists(self, artist_ids: Sequence[str]) -> list[SpotifyArtist]:
        """Batched GET /artists. Genres still live on the artist object."""
        found: list[SpotifyArtist] = []
        for chunk in _chunks(artist_ids, TRACK_BATCH_SIZE):
            payload = self._get_json(f"{API_ROOT}/artists", params={"ids": ",".join(chunk)})
            rows = payload.get("artists")
            if not isinstance(rows, list):
                raise SpotifyClientError("Spotify returned an unexpected artists response.")
            for row in rows:
                if not row:
                    continue
                try:
                    found.append(SpotifyArtist.model_validate(row))
                except ValidationError as exc:
                    raise SpotifyClientError(
                        "Spotify returned an unexpected artists response."
                    ) from exc
        return found

    def search_tracks(
        self,
        query: str,
        *,
        market: str | None = None,
        limit: int = 5,
    ) -> list[SpotifyTrack]:
        """GET /search?type=track, with the user's market."""
        params: dict[str, str | int] = {"q": query, "type": "track", "limit": limit}
        if market:
            params["market"] = market
        payload = self._get_json(SEARCH_URL, params=params)
        try:
            page = _SearchPayload.model_validate(payload)
        except ValidationError as exc:
            raise SpotifyClientError("Spotify returned an unexpected search response.") from exc
        if page.tracks is None:
            return []
        return page.tracks.items

    def create_playlist(self, user_id: str, name: str, *, description: str = "") -> str:
        """POST /users/{id}/playlists. Private. Returns the new playlist id."""
        payload = self._send_json(
            "POST",
            f"{API_ROOT}/users/{user_id}/playlists",
            json_body={
                "name": name,
                "public": False,
                "collaborative": False,
                "description": description,
            },
        )
        try:
            return SpotifyPlaylist.model_validate(payload).id
        except ValidationError as exc:
            raise SpotifyClientError("Spotify returned an unexpected playlist response.") from exc

    def add_playlist_tracks(self, playlist_id: str, uris: Sequence[str]) -> None:
        """POST /playlists/{id}/tracks."""
        self._send_json(
            "POST",
            f"{API_ROOT}/playlists/{playlist_id}/tracks",
            json_body={"uris": list(uris)},
        )

    def replace_playlist_tracks(self, playlist_id: str, uris: Sequence[str]) -> None:
        """PUT /playlists/{id}/tracks. Replaces the list so a retry stays idempotent."""
        self._send_json(
            "PUT",
            f"{API_ROOT}/playlists/{playlist_id}/tracks",
            json_body={"uris": list(uris)},
        )

    def find_playlist_id(self, name: str) -> str | None:
        """Return the id of a playlist with this exact name, if the user has one."""
        pages = self._collect_pages(
            PLAYLISTS_URL,
            params={"limit": 50},
            model=_PlaylistPage,
            failure="Spotify returned an unexpected playlists response.",
        )
        for playlist in pages:
            if isinstance(playlist, SpotifyPlaylist) and playlist.name == name:
                return playlist.id
        return None

    def _collect_pages(
        self,
        url: str,
        *,
        params: _Query,
        model: type[_ArtistPage] | type[_TrackPage] | type[_AlbumPage] | type[_PlaylistPage],
        failure: str,
    ) -> list[SpotifyArtist] | list[SpotifyTrack] | list[SpotifyAlbum] | list[SpotifyPlaylist]:
        found: list[SpotifyArtist | SpotifyTrack | SpotifyAlbum | SpotifyPlaylist] = []
        next_url: str | None = url
        query: _Query | None = params
        pages = 0
        while next_url is not None:
            pages += 1
            if pages > _MAX_PAGES:
                raise SpotifyClientError("Spotify pagination did not end.")
            payload = self._get_json(next_url, params=query)
            try:
                page = model.model_validate(payload)
            except ValidationError as exc:
                raise SpotifyClientError(failure) from exc
            found.extend(page.items)
            next_url = _next_page(page.next)
            query = None
        return found

    def _get_json(self, url: str, *, params: _Query | None) -> dict[str, object]:
        with self._session() as http:
            return self._request_json(http, "GET", url, params, json_body=None)

    def _send_json(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, object],
    ) -> dict[str, object]:
        with self._session() as http:
            return self._request_json(http, method, url, None, json_body=json_body)

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
        method: str,
        url: str,
        params: _Query | None,
        *,
        json_body: dict[str, object] | None,
    ) -> dict[str, object]:
        refreshed = False
        rate_limits = 0
        token_override: str | None = None
        while True:
            token = token_override if token_override is not None else self._token()
            response = http.request(
                method,
                url,
                params=params,
                json=json_body,
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
            if response.status_code not in (200, 201):
                raise SpotifyClientError(f"Spotify request failed ({response.status_code}).")
            try:
                body = response.json()
            except ValueError as exc:
                raise SpotifyClientError("Spotify returned an unexpected response.") from exc
            if not isinstance(body, dict):
                raise SpotifyClientError("Spotify returned an unexpected response.")
            return body


def _require_time_range(time_range: str) -> None:
    if time_range not in _TIME_RANGES:
        raise SpotifyClientError(f"Unknown Spotify time range: {time_range}.")


def _chunks(values: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


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
