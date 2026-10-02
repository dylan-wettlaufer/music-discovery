"""Resolve Last.fm names to Spotify track ids."""

import logging
from collections.abc import Sequence
from datetime import date, datetime, timezone
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from music_discovery.normalize import normalize
from music_discovery.models import Resolution
from music_discovery.pipeline.candidates import NamedTrack, genre_jaccard, release_recency
from music_discovery.pipeline.types import Candidate
from music_discovery.spotify.client import SpotifyClient, SpotifyTrack

logger = logging.getLogger("music_discovery.pipeline.resolve")

ACCEPT_THRESHOLD = 0.8
COMPILATION_PENALTY = 0.2


def resolve_candidates(
    session: Session,
    client: SpotifyClient,
    named: Sequence[NamedTrack],
    *,
    market: str | None,
    seed_genres: set[str],
    today: date,
) -> tuple[list[Candidate], int]:
    """Search ``track`` and ``artist`` in the user's market.

    Accept only a high-confidence hit. Persist hits and misses on ``resolutions``.
    """
    misses = 0
    pending: list[tuple[NamedTrack, str]] = []
    found_tracks: dict[str, SpotifyTrack] = {}
    seen: set[tuple[str, str]] = set()
    for item in named:
        artist_key = normalize(item.artist)
        title_key = normalize(item.title)
        identity = (artist_key, title_key)
        if not artist_key or not title_key or identity in seen:
            continue
        seen.add(identity)
        cached = session.scalar(
            select(Resolution).where(
                Resolution.normalized_artist == artist_key,
                Resolution.normalized_title == title_key,
            )
        )
        if cached is not None:
            if cached.spotify_track_id:
                logger.info("Cached %s — %s", item.artist, item.title)
                pending.append((item, cached.spotify_track_id))
            else:
                logger.info("Cached miss %s — %s", item.artist, item.title)
                misses += 1
            continue
        logger.info("Search %s — %s", item.artist, item.title)
        chosen = _best_hit(
            item,
            client.search_tracks(_search_query(item.artist, item.title), market=market, limit=5),
        )
        session.add(
            Resolution(
                normalized_artist=artist_key,
                normalized_title=title_key,
                spotify_track_id=chosen.id if chosen is not None else None,
                fetched_at=datetime.now(timezone.utc),
            )
        )
        if chosen is None or not chosen.id:
            logger.info("No Spotify match %s — %s", item.artist, item.title)
            misses += 1
            continue
        logger.info("Matched %s — %s", item.artist, item.title)
        found_tracks[chosen.id] = chosen
        pending.append((item, chosen.id))
    session.flush()

    seed_genre_set = {_genre(genre) for genre in seed_genres}
    resolved: list[Candidate] = []
    for item, track_id in pending:
        track = found_tracks.get(track_id)
        if track is None:
            resolved.append(
                Candidate(
                    spotify_track_id=track_id,
                    artist=item.artist,
                    title=item.title,
                    source=item.source,
                    similarity=item.similarity,
                    genre_overlap=0.0,
                    recency=0.0,
                    seed_id=item.seed_id,
                )
            )
            continue
        primary = track.artists[0].name.strip() if track.artists else item.artist
        if not primary or not track.name.strip():
            misses += 1
            continue
        artist_genres: set[str] = set()
        for artist in track.artists:
            artist_genres.update(_genre(genre) for genre in artist.genres)
        release = track.album.release_date if track.album is not None else None
        resolved.append(
            Candidate(
                spotify_track_id=track_id,
                artist=primary,
                title=track.name.strip(),
                source=item.source,
                similarity=item.similarity,
                genre_overlap=genre_jaccard(artist_genres, seed_genre_set),
                recency=release_recency(release, today=today),
                popularity=track.popularity,
                seed_id=item.seed_id,
            )
        )
    return resolved, misses


def _search_query(artist: str, title: str) -> str:
    return f'track:"{_escape(title)}" artist:"{_escape(artist)}"'


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _best_hit(item: NamedTrack, hits: Sequence[SpotifyTrack]) -> SpotifyTrack | None:
    winner: SpotifyTrack | None = None
    best = 0.0
    for hit in hits:
        score = _match_score(item.artist, item.title, hit)
        if score > best:
            best = score
            winner = hit
    if winner is None or best < ACCEPT_THRESHOLD:
        return None
    return winner


def _match_score(artist: str, title: str, track: SpotifyTrack) -> float:
    primary = track.artists[0].name if track.artists else ""
    if normalize(primary) != normalize(artist):
        return 0.0
    score = SequenceMatcher(None, normalize(title), normalize(track.name)).ratio()
    if track.album is not None and track.album.album_type == "compilation":
        score -= COMPILATION_PENALTY
    return score


def _genre(value: str) -> str:
    return value.casefold().strip()
