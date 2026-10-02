"""Last.fm similarity.

Throttle to about 4 requests per second. Error 29 and HTTP 429 back off.
Read ``lastfm_cache`` before every call. ``artist.getSimilar`` match is 0–1.
``track.getSimilar`` match is an unbounded float. ``artist.getTopTracks``
returns playcount, not similarity.
"""

import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from music_discovery.models import LastfmCache

logger = logging.getLogger("music_discovery.lastfm")

API_URL = "https://ws.audioscrobbler.com/2.0/"
_MAX_BACKOFFS = 4
_DEFAULT_TTL = timedelta(days=30)

Sleeper = Callable[[float], None]
Clock = Callable[[], datetime]


class LastFmError(Exception):
    """A Last.fm read failed after the allowed retries."""


@dataclass(frozen=True)
class SimilarArtist:
    name: str
    match: float


@dataclass(frozen=True)
class TopTrack:
    name: str
    artist: str
    playcount: int
    rank: int


@dataclass(frozen=True)
class SimilarTrack:
    name: str
    artist: str
    match: float


class LastFmClient:
    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx.Client | None = None,
        sleep: Sleeper | None = None,
        requests_per_second: float = 4.0,
        session: Session | None = None,
        cache_ttl: timedelta | None = None,
        now: Clock | None = None,
    ) -> None:
        self._api_key = api_key
        self._http = http_client
        self._sleep = sleep or time.sleep
        self._interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._session = session
        self._cache_ttl = cache_ttl if cache_ttl is not None else _DEFAULT_TTL
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._last_request: float | None = None

    def get_similar_artists(self, artist: str, *, limit: int = 20) -> list[SimilarArtist]:
        """artist.getSimilar with autocorrect=1."""
        body = self._call(
            "artist.getSimilar",
            {"artist": artist, "limit": str(limit), "autocorrect": "1"},
        )
        artists = _as_list(_section(body, "similarartists").get("artist"))
        found: list[SimilarArtist] = []
        for item in artists:
            if not isinstance(item, dict):
                continue
            name = _text(item.get("name"))
            if not name:
                continue
            found.append(SimilarArtist(name=name, match=_number(item.get("match"))))
        return found

    def get_top_tracks(self, artist: str, *, limit: int = 10) -> list[TopTrack]:
        """artist.getTopTracks. Used to drop obvious hits from deep cuts."""
        body = self._call(
            "artist.getTopTracks",
            {"artist": artist, "limit": str(limit), "autocorrect": "1"},
        )
        tracks = _as_list(_section(body, "toptracks").get("track"))
        found: list[TopTrack] = []
        for item in tracks:
            if not isinstance(item, dict):
                continue
            name = _text(item.get("name"))
            if not name:
                continue
            rank = item.get("@attr", {})
            rank_value = rank.get("rank") if isinstance(rank, dict) else None
            found.append(
                TopTrack(
                    name=name,
                    artist=_artist_name(item.get("artist")) or artist,
                    playcount=int(_number(item.get("playcount"))),
                    rank=int(_number(rank_value)) or len(found) + 1,
                )
            )
        return found

    def get_similar_tracks(self, artist: str, track: str, *, limit: int = 20) -> list[SimilarTrack]:
        """track.getSimilar. Keep the raw match for per-source normalization."""
        body = self._call(
            "track.getSimilar",
            {
                "artist": artist,
                "track": track,
                "limit": str(limit),
                "autocorrect": "1",
            },
        )
        tracks = _as_list(_section(body, "similartracks").get("track"))
        found: list[SimilarTrack] = []
        for item in tracks:
            if not isinstance(item, dict):
                continue
            name = _text(item.get("name"))
            artist_name = _artist_name(item.get("artist"))
            if not name or not artist_name:
                continue
            found.append(
                SimilarTrack(name=name, artist=artist_name, match=_number(item.get("match")))
            )
        return found

    def _call(self, method: str, params: dict[str, str]) -> dict[str, Any]:
        cached = self._read_cache(method, params)
        if cached is not None:
            return cached
        body = self._fetch(method, params)
        self._write_cache(method, params, body)
        return body

    def _fetch(self, method: str, params: Mapping[str, str]) -> dict[str, Any]:
        if self._http is None:
            raise LastFmError("Last.fm client has no HTTP transport.")
        attempt = 0
        while True:
            self._throttle()
            response = self._http.get(
                API_URL,
                params={
                    "method": method,
                    "api_key": self._api_key,
                    "format": "json",
                    **params,
                },
            )
            body = _json_object(response)
            if response.status_code == 429 or _rate_limited(body):
                if attempt >= _MAX_BACKOFFS:
                    raise LastFmError("Last.fm rate limit persisted.")
                self._sleep(_backoff(response, attempt))
                attempt += 1
                continue
            if response.status_code != 200:
                error = LastFmError(f"Last.fm request failed ({response.status_code}) {method}.")
                logger.error("%s", error)
                raise error
            if isinstance(body.get("error"), int):
                message = body.get("message")
                detail = message if isinstance(message, str) and message else "Last.fm request failed."
                error = LastFmError(f"{detail} ({method}).")
                logger.error("%s", error)
                raise error
            logger.info("Last.fm %s ok", method)
            return body

    def _throttle(self) -> None:
        now = time.monotonic()
        if self._last_request is not None and self._interval:
            wait = self._interval - (now - self._last_request)
            if wait > 0:
                self._sleep(wait)
                now = time.monotonic()
        self._last_request = now

    def _read_cache(self, method: str, params: Mapping[str, str]) -> dict[str, Any] | None:
        if self._session is None:
            return None
        row = self._session.scalar(
            select(LastfmCache).where(
                LastfmCache.method == method,
                LastfmCache.params_hash == _params_hash(method, params),
            )
        )
        if row is None:
            return None
        fetched = row.fetched_at
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        if self._now() - fetched > self._cache_ttl:
            return None
        body = row.body
        if isinstance(body, str):
            loaded = json.loads(body)
            if not isinstance(loaded, dict):
                return None
            return loaded
        return body

    def _write_cache(self, method: str, params: Mapping[str, str], body: dict[str, Any]) -> None:
        if self._session is None:
            return
        digest = _params_hash(method, params)
        existing = self._session.scalar(
            select(LastfmCache).where(
                LastfmCache.method == method,
                LastfmCache.params_hash == digest,
            )
        )
        if existing is None:
            self._session.add(
                LastfmCache(
                    method=method,
                    params_hash=digest,
                    body=body,
                    fetched_at=self._now(),
                )
            )
        else:
            existing.body = body
            existing.fetched_at = self._now()
        self._session.flush()


def _params_hash(method: str, params: Mapping[str, str]) -> str:
    payload = json.dumps(
        {"method": method, "params": dict(params)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _section(body: dict[str, Any], key: str) -> dict[str, Any]:
    value = body.get(key)
    if isinstance(value, dict):
        return value
    return {}


def _as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _artist_name(value: object) -> str:
    if isinstance(value, dict):
        return _text(value.get("name"))
    return _text(value)


def _number(value: object) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    if isinstance(body, dict):
        return body
    return {}


def _rate_limited(body: Mapping[str, Any]) -> bool:
    return body.get("error") == 29


def _backoff(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("retry-after")
    if raw is not None:
        try:
            return max(0.0, float(raw))
        except ValueError:
            return 1.0
    return min(60.0, float(2**attempt))
