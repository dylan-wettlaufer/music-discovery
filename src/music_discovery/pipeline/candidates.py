"""Candidate sources: similar artists, similar tracks, and deep cuts."""

import logging
from dataclasses import dataclass
from datetime import date

from music_discovery.lastfm.client import LastFmClient
from music_discovery.normalize import normalize
from music_discovery.pipeline.score import inverse_popularity
from music_discovery.pipeline.seeds import SeedSet
from music_discovery.pipeline.types import Candidate, Source
from music_discovery.spotify.client import SpotifyClient

logger = logging.getLogger("music_discovery.pipeline.candidates")

SIMILAR_ARTIST_LIMIT = 20
SIMILAR_ARTISTS_KEPT = 15
TOP_TRACKS_PER_ARTIST = 5
SIMILAR_TRACK_LIMIT = 10
DEEP_CUT_ALBUMS = 3
DEEP_CUT_TRACKS = 8
HIT_POPULARITY = 70
_RECENCY_YEARS = 15


@dataclass(frozen=True)
class NamedTrack:
    """A Last.fm candidate that still needs a Spotify id."""

    artist: str
    title: str
    source: Source
    similarity: float
    seed_id: str | None


def similar_artist_candidates(lastfm: LastFmClient, seeds: SeedSet) -> list[NamedTrack]:
    """Last.fm ``artist.getSimilar``, then ``artist.getTopTracks``.

    Similarity is the artist match (0–1) times a rank decay.
    """
    seed_names = {normalize(artist.name) for artist in seeds.artists}
    best: dict[str, tuple[float, str, str]] = {}
    artists = seeds.artists
    for index, artist in enumerate(artists, start=1):
        logger.info("Similar artists %s/%s %s", index, len(artists), artist.name)
        for similar in lastfm.get_similar_artists(artist.name, limit=SIMILAR_ARTIST_LIMIT):
            key = normalize(similar.name)
            if not key or key in seed_names:
                continue
            current = best.get(key)
            if current is None or similar.match > current[0]:
                best[key] = (similar.match, similar.name, artist.spotify_id)
    ranked = sorted(best.values(), key=lambda item: item[0], reverse=True)[:SIMILAR_ARTISTS_KEPT]
    found: list[NamedTrack] = []
    for match, name, seed_id in ranked:
        tracks = lastfm.get_top_tracks(name, limit=TOP_TRACKS_PER_ARTIST)
        for rank, track in enumerate(tracks, start=1):
            title = track.name.strip()
            artist_name = track.artist.strip() or name
            if not title or not artist_name:
                continue
            found.append(
                NamedTrack(
                    artist=artist_name,
                    title=title,
                    source=Source.SIMILAR_ARTIST,
                    similarity=match / rank,
                    seed_id=seed_id,
                )
            )
    return found


def similar_track_candidates(lastfm: LastFmClient, seeds: SeedSet) -> list[NamedTrack]:
    """Last.fm ``track.getSimilar``. Store the raw match; normalize it later."""
    found: list[NamedTrack] = []
    seen: set[str] = set()
    tracks = seeds.tracks
    for index, track in enumerate(tracks, start=1):
        logger.info("Similar tracks %s/%s %s — %s", index, len(tracks), track.artist, track.title)
        for similar in lastfm.get_similar_tracks(
            track.artist,
            track.title,
            limit=SIMILAR_TRACK_LIMIT,
        ):
            artist_name = similar.artist.strip()
            title = similar.name.strip()
            if not artist_name or not title:
                continue
            key = f"{normalize(artist_name)}\n{normalize(title)}"
            if key in seen:
                continue
            seen.add(key)
            found.append(
                NamedTrack(
                    artist=artist_name,
                    title=title,
                    source=Source.SIMILAR_TRACK,
                    similarity=similar.match,
                    seed_id=track.spotify_id,
                )
            )
    return found


def deep_cut_candidates(
    spotify: SpotifyClient,
    lastfm: LastFmClient,
    seeds: SeedSet,
    *,
    today: date,
) -> list[Candidate]:
    """Album tracks from artists the user already likes, with hits removed."""
    found: list[Candidate] = []
    artists = seeds.artists
    for index, artist in enumerate(artists, start=1):
        logger.info("Deep cuts %s/%s %s", index, len(artists), artist.name)
        hit_names = {
            normalize(track.name)
            for track in lastfm.get_top_tracks(artist.name, limit=TOP_TRACKS_PER_ARTIST)
        }
        albums = [
            album
            for album in spotify.get_artist_albums(artist.spotify_id, limit=DEEP_CUT_ALBUMS)
            if album.id and album.album_type != "compilation"
        ][:DEEP_CUT_ALBUMS]
        ordered: list[tuple[SpotifyTrack, str | None]] = []
        seen_ids: set[str] = set()
        for album in albums:
            if album.id is None:
                continue
            for track in spotify.get_album_tracks(album.id, limit=50):
                if not track.id or track.id in seen_ids:
                    continue
                seen_ids.add(track.id)
                ordered.append((track, album.release_date))
        kept = 0
        for track, album_release in ordered:
            if kept >= DEEP_CUT_TRACKS:
                break
            if not track.id or not track.name.strip():
                continue
            if track.popularity is not None and track.popularity >= HIT_POPULARITY:
                continue
            if normalize(track.name) in hit_names:
                continue
            primary = track.artists[0].name.strip() if track.artists else artist.name
            if not primary:
                continue
            release = None
            if track.album is not None and track.album.release_date:
                release = track.album.release_date
            else:
                release = album_release
            found.append(
                Candidate(
                    spotify_track_id=track.id,
                    artist=primary,
                    title=track.name.strip(),
                    source=Source.DEEP_CUT,
                    similarity=inverse_popularity(track.popularity),
                    genre_overlap=1.0,
                    recency=release_recency(release, today=today),
                    popularity=track.popularity,
                    seed_id=artist.spotify_id,
                )
            )
            kept += 1
        logger.info("Deep cuts %s kept %s", artist.name, kept)
    return found


def genre_jaccard(left: set[str], right: set[str]) -> float:
    """Overlap between a candidate's genres and the seed-artist genre set."""
    if not left or not right:
        return 0.0
    shared = left & right
    return len(shared) / len(left | right)


def release_recency(release_date: str | None, *, today: date) -> float:
    """Mild 0–1 boost for a release inside the last 15 years."""
    year = _year(release_date)
    if year is None:
        return 0.0
    return min(1.0, max(0.0, (year - (today.year - _RECENCY_YEARS)) / _RECENCY_YEARS))


def _year(release_date: str | None) -> int | None:
    if release_date is None or len(release_date) < 4:
        return None
    head = release_date[:4]
    if not head.isdigit():
        return None
    return int(head)
