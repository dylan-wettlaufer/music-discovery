import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from music_discovery.lastfm.client import API_URL, LastFmClient, LastFmError
from music_discovery.models import LastfmCache

_NOW = datetime(2026, 10, 2, 15, tzinfo=timezone.utc)


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(element, compiler, **kwargs):
    del element, kwargs
    return "JSON"


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kwargs):
    del element, kwargs
    return "JSON"


def _dump_value(value: object) -> object:
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return value


def _dump_parameters(parameters: object) -> object:
    if isinstance(parameters, dict):
        return {key: _dump_value(value) for key, value in parameters.items()}
    if isinstance(parameters, tuple):
        return tuple(_dump_value(value) for value in parameters)
    if isinstance(parameters, list):
        return [_dump_parameters(value) for value in parameters]
    return parameters


@pytest.fixture
def cache_db():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, connection_record) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _json_bind(conn, cursor, statement, parameters, context, executemany):
        del conn, cursor, context, executemany
        return statement, _dump_parameters(parameters)

    LastfmCache.__table__.create(engine)

    @contextmanager
    def sessions() -> Iterator[Session]:
        session = Session(engine)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return sessions


def _client(handler, **kwargs) -> tuple[httpx.Client, LastFmClient]:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return http, LastFmClient("lastfm-key", http_client=http, requests_per_second=0, **kwargs)


def test_similar_artists_keep_the_match_and_autocorrect():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url.copy_with(query=None)) == API_URL
        seen["method"] = request.url.params["method"]
        seen["autocorrect"] = request.url.params["autocorrect"]
        seen["limit"] = request.url.params["limit"]
        seen["key"] = request.url.params["api_key"]
        return httpx.Response(
            200,
            json={
                "similarartists": {
                    "artist": [
                        {"name": "Portishead", "match": "0.82"},
                        {"name": "Massive Attack", "match": 0.4},
                    ]
                }
            },
        )

    http, client = _client(handler)
    with http:
        artists = client.get_similar_artists("Radiohead", limit=20)

    assert seen == {
        "method": "artist.getSimilar",
        "autocorrect": "1",
        "limit": "20",
        "key": "lastfm-key",
    }
    assert [(artist.name, artist.match) for artist in artists] == [
        ("Portishead", 0.82),
        ("Massive Attack", 0.4),
    ]


def test_a_single_similar_artist_object_is_still_a_list():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={"similarartists": {"artist": {"name": "Portishead", "match": "1"}}},
        )

    http, client = _client(handler)
    with http:
        artists = client.get_similar_artists("Radiohead")

    assert artists[0].name == "Portishead"
    assert artists[0].match == 1.0


def test_top_tracks_and_similar_tracks_keep_raw_values():
    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.params["method"]
        if method == "artist.getTopTracks":
            return httpx.Response(
                200,
                json={
                    "toptracks": {
                        "track": [
                            {
                                "name": "Creep",
                                "playcount": "100",
                                "artist": {"name": "Radiohead"},
                                "@attr": {"rank": "1"},
                            }
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "similartracks": {
                    "track": [
                        {"name": "Glory Box", "match": 12.5, "artist": {"name": "Portishead"}}
                    ]
                }
            },
        )

    http, client = _client(handler)
    with http:
        tops = client.get_top_tracks("Radiohead", limit=5)
        similar = client.get_similar_tracks("Radiohead", "Karma Police", limit=10)

    assert tops[0].name == "Creep"
    assert tops[0].rank == 1
    assert tops[0].playcount == 100
    assert similar[0].match == 12.5
    assert similar[0].artist == "Portishead"


def test_cache_is_read_before_the_network(cache_db):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        calls["count"] += 1
        return httpx.Response(
            200,
            json={"similarartists": {"artist": [{"name": "Portishead", "match": "0.5"}]}},
        )

    http, _ = _client(handler)
    with http:
        with cache_db() as session:
            client = LastFmClient(
                "lastfm-key",
                http_client=http,
                requests_per_second=0,
                session=session,
                now=lambda: _NOW,
            )
            first = client.get_similar_artists("Radiohead")
            second = client.get_similar_artists("Radiohead")
            stored = session.scalars(select(LastfmCache)).all()

    assert [artist.name for artist in first] == ["Portishead"]
    assert [artist.name for artist in second] == ["Portishead"]
    assert calls["count"] == 1
    assert len(stored) == 1


def test_an_expired_cache_row_is_fetched_again(cache_db):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        calls["count"] += 1
        return httpx.Response(
            200,
            json={"similarartists": {"artist": [{"name": "Portishead", "match": "0.5"}]}},
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with http:
        with cache_db() as session:
            client = LastFmClient(
                "lastfm-key",
                http_client=http,
                requests_per_second=0,
                session=session,
                cache_ttl=timedelta(days=30),
                now=lambda: _NOW,
            )
            client.get_similar_artists("Radiohead")
            client._now = lambda: _NOW + timedelta(days=31)
            client.get_similar_artists("Radiohead")

    assert calls["count"] == 2


def test_calls_wait_for_the_rate_limit():
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"similarartists": {"artist": []}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = LastFmClient(
        "lastfm-key",
        http_client=http,
        requests_per_second=4,
        sleep=delays.append,
    )
    with http:
        client.get_similar_artists("Radiohead")
        client.get_similar_artists("Portishead")

    assert delays
    assert delays[0] == pytest.approx(0.25, abs=0.05)


def test_error_29_backs_off_and_then_reads_the_payload():
    delays: list[float] = []
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(200, json={"error": 29, "message": "Rate limit exceeded"})
        return httpx.Response(
            200,
            json={"similarartists": {"artist": [{"name": "Portishead", "match": "0.2"}]}},
        )

    http, client = _client(handler, sleep=delays.append)
    with http:
        artists = client.get_similar_artists("Radiohead")

    assert artists[0].name == "Portishead"
    assert delays == [1.0]
    assert calls["count"] == 2


def test_http_429_uses_retry_after():
    delays: list[float] = []
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, json={"error": 29})
        return httpx.Response(200, json={"similarartists": {"artist": []}})

    http, client = _client(handler, sleep=delays.append)
    with http:
        assert client.get_similar_artists("Radiohead") == []

    assert delays == [2.0]


def test_a_persistent_rate_limit_stops():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"error": 29, "message": "Rate limit exceeded"})

    http, client = _client(handler, sleep=lambda delay: None)
    with http:
        with pytest.raises(LastFmError, match="rate limit"):
            client.get_similar_artists("Radiohead")
