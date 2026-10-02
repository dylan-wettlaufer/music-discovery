"""Seed gathering from top artists, top tracks, and a sample of saved tracks."""

from collections.abc import Sequence
from dataclasses import dataclass

from music_discovery.spotify.client import SpotifyClient

MAX_SEED_ARTISTS = 30
MAX_SEED_TRACKS = 40
SHORT_TERM_ARTISTS = 5
SHORT_TERM_TRACKS = 5


@dataclass(frozen=True)
class ArtistSeed:
    spotify_id: str
    name: str
    genres: frozenset[str]


@dataclass(frozen=True)
class TrackSeed:
    spotify_id: str
    artist: str
    title: str
    artist_id: str | None


@dataclass(frozen=True)
class SavedSeed:
    spotify_id: str
    artist: str
    title: str
    artist_id: str | None


@dataclass(frozen=True)
class SeedSet:
    artists: tuple[ArtistSeed, ...]
    tracks: tuple[TrackSeed, ...]


def gather_seeds(client: SpotifyClient, saved: Sequence[SavedSeed] = ()) -> SeedSet:
    """Top artists, top tracks, and a sample of saved tracks.

    Caps are about 30 artists and 40 tracks. ``medium_term`` and ``long_term``
    lead; ``short_term`` is only a light addition.
    """
    artists = _artist_seeds(client)
    tracks = _track_seeds(client, saved)
    artists = _fill_artists_from_tracks(client, artists, tracks)
    return SeedSet(artists=tuple(artists), tracks=tuple(tracks))


def _artist_seeds(client: SpotifyClient) -> list[ArtistSeed]:
    found: list[ArtistSeed] = []
    seen: set[str] = set()
    _take_artists(
        found,
        seen,
        client.get_top_artists(time_range="medium_term"),
        limit=MAX_SEED_ARTISTS,
    )
    _take_artists(
        found,
        seen,
        client.get_top_artists(time_range="long_term"),
        limit=MAX_SEED_ARTISTS,
    )
    _take_artists(
        found,
        seen,
        client.get_top_artists(time_range="short_term"),
        limit=SHORT_TERM_ARTISTS,
    )
    return found


def _take_artists(found: list[ArtistSeed], seen: set[str], artists, *, limit: int) -> None:
    added = 0
    for artist in artists:
        if len(found) >= MAX_SEED_ARTISTS or added >= limit:
            return
        if not artist.id or not artist.name.strip() or artist.id in seen:
            continue
        seen.add(artist.id)
        found.append(
            ArtistSeed(
                spotify_id=artist.id,
                name=artist.name.strip(),
                genres=frozenset(genre for genre in artist.genres if genre),
            )
        )
        added += 1


def _track_seeds(client: SpotifyClient, saved: Sequence[SavedSeed]) -> list[TrackSeed]:
    found: list[TrackSeed] = []
    seen: set[str] = set()
    _take_tracks(found, seen, client.get_top_tracks(time_range="medium_term"), limit=MAX_SEED_TRACKS)
    _take_tracks(found, seen, client.get_top_tracks(time_range="long_term"), limit=MAX_SEED_TRACKS)
    _take_tracks(found, seen, client.get_top_tracks(time_range="short_term"), limit=SHORT_TERM_TRACKS)
    room = MAX_SEED_TRACKS - len(found)
    for saved_track in _sample(saved, room):
        if saved_track.spotify_id in seen:
            continue
        seen.add(saved_track.spotify_id)
        found.append(
            TrackSeed(
                spotify_id=saved_track.spotify_id,
                artist=saved_track.artist,
                title=saved_track.title,
                artist_id=saved_track.artist_id,
            )
        )
        if len(found) >= MAX_SEED_TRACKS:
            break
    return found


def _take_tracks(found: list[TrackSeed], seen: set[str], tracks, *, limit: int) -> None:
    added = 0
    for track in tracks:
        if len(found) >= MAX_SEED_TRACKS or added >= limit:
            return
        if not track.id or not track.name.strip() or track.id in seen:
            continue
        artist_name = track.artists[0].name.strip() if track.artists else ""
        if not artist_name:
            continue
        artist_id = track.artists[0].id if track.artists else None
        seen.add(track.id)
        found.append(
            TrackSeed(
                spotify_id=track.id,
                artist=artist_name,
                title=track.name.strip(),
                artist_id=artist_id,
            )
        )
        added += 1


def _fill_artists_from_tracks(
    client: SpotifyClient,
    artists: list[ArtistSeed],
    tracks: Sequence[TrackSeed],
) -> list[ArtistSeed]:
    seen = {artist.spotify_id for artist in artists}
    for track in tracks:
        if len(artists) >= MAX_SEED_ARTISTS:
            break
        if not track.artist_id or track.artist_id in seen:
            continue
        seen.add(track.artist_id)
        artists.append(ArtistSeed(spotify_id=track.artist_id, name=track.artist, genres=frozenset()))
    missing = [artist.spotify_id for artist in artists if not artist.genres]
    if not missing:
        return artists
    looked_up = {
        artist.id: frozenset(genre for genre in artist.genres if genre)
        for artist in client.get_artists(missing)
        if artist.id
    }
    return [
        ArtistSeed(
            spotify_id=artist.spotify_id,
            name=artist.name,
            genres=looked_up.get(artist.spotify_id, artist.genres),
        )
        for artist in artists
    ]


def _sample[T](items: Sequence[T], count: int) -> list[T]:
    """Spread ``count`` picks across the library instead of taking only the head."""
    if count <= 0 or not items:
        return []
    if len(items) <= count:
        return list(items)
    step = len(items) / count
    picked: list[T] = []
    used: set[int] = set()
    cursor = 0.0
    while len(picked) < count and len(used) < len(items):
        index = min(len(items) - 1, int(cursor))
        while index in used and index + 1 < len(items):
            index += 1
        if index in used:
            break
        used.add(index)
        picked.append(items[index])
        cursor += step
    return picked
