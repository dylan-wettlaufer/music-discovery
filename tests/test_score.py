import pytest

from music_discovery.pipeline.score import inverse_popularity, score_candidates, select_tracks
from music_discovery.pipeline.types import Candidate, Source


def _candidate(
    track_id: str,
    *,
    source: Source,
    similarity: float = 0.0,
    genre_overlap: float = 0.0,
    recency: float = 0.0,
    popularity: int | None = None,
    artist: str | None = None,
    seed_id: str | None = None,
) -> Candidate:
    return Candidate(
        spotify_track_id=track_id,
        artist=artist or f"Artist {track_id}",
        title=f"Title {track_id}",
        source=source,
        similarity=similarity,
        genre_overlap=genre_overlap,
        recency=recency,
        popularity=popularity,
        seed_id=seed_id if seed_id is not None else track_id,
    )


def _by_id(scored):
    return {item.candidate.spotify_track_id: item for item in scored}


def test_weights_use_per_source_normalized_similarity():
    scored = _by_id(
        score_candidates(
            [
                _candidate(
                    "all",
                    source=Source.SIMILAR_ARTIST,
                    similarity=1,
                    genre_overlap=1,
                    recency=1,
                ),
                _candidate("sim", source=Source.SIMILAR_ARTIST, similarity=1),
                _candidate("genre", source=Source.SIMILAR_ARTIST, genre_overlap=1),
                _candidate("recency", source=Source.SIMILAR_ARTIST, recency=1),
            ]
        )
    )

    assert scored["all"].score == pytest.approx(1.0)
    assert scored["sim"].score == pytest.approx(0.55)
    assert scored["genre"].score == pytest.approx(0.30)
    assert scored["recency"].score == pytest.approx(0.15)
    assert scored["sim"].components.similarity == pytest.approx(1.0)
    assert scored["genre"].components.similarity == pytest.approx(0.0)


def test_similar_track_matches_are_normalized_before_scoring():
    scored = _by_id(
        score_candidates(
            [
                _candidate("low", source=Source.SIMILAR_TRACK, similarity=0),
                _candidate("high", source=Source.SIMILAR_TRACK, similarity=10.95),
            ]
        )
    )

    assert scored["low"].components.similarity == pytest.approx(0.0)
    assert scored["high"].components.similarity == pytest.approx(1.0)
    assert scored["high"].score == pytest.approx(0.55)


def test_deep_cuts_use_inverse_popularity():
    assert inverse_popularity(0) == pytest.approx(1.0)
    assert inverse_popularity(100) == pytest.approx(0.0)
    assert inverse_popularity(None) == pytest.approx(0.0)

    scored = _by_id(
        score_candidates(
            [
                _candidate("obscure", source=Source.DEEP_CUT, similarity=99, popularity=0),
                _candidate("hit", source=Source.DEEP_CUT, similarity=99, popularity=100),
            ]
        )
    )

    assert scored["obscure"].components.similarity == pytest.approx(1.0)
    assert scored["hit"].components.similarity == pytest.approx(0.0)
    assert scored["obscure"].score > scored["hit"].score


def test_quotas_fill_before_backfill():
    artists = [
        _candidate(f"artist-{index}", source=Source.SIMILAR_ARTIST, similarity=30 - index)
        for index in range(12)
    ]
    tracks = [
        _candidate(f"track-{index}", source=Source.SIMILAR_TRACK, similarity=1)
        for index in range(10)
    ]
    cuts = [
        _candidate(f"cut-{index}", source=Source.DEEP_CUT, popularity=40)
        for index in range(5)
    ]

    selection = select_tracks(score_candidates([*artists, *tracks, *cuts]))
    ids = [item.candidate.spotify_track_id for item in selection.tracks]
    sources = [item.candidate.source for item in selection.tracks]

    assert selection.publishable
    assert sources == (
        [Source.SIMILAR_ARTIST] * 10 + [Source.SIMILAR_TRACK] * 10 + [Source.DEEP_CUT] * 5
    )
    assert ids[:10] == [f"artist-{index}" for index in range(10)]
    assert "artist-10" not in ids
    assert "artist-11" not in ids


def test_thin_source_backfills_to_the_playlist_size():
    artists = [
        _candidate(f"artist-{index}", source=Source.SIMILAR_ARTIST, similarity=40 - index)
        for index in range(30)
    ]

    selection = select_tracks(score_candidates(artists))
    ids = [item.candidate.spotify_track_id for item in selection.tracks]

    assert len(ids) == 25
    assert ids == [f"artist-{index}" for index in range(25)]


def test_short_pool_is_not_published():
    tracks = [
        _candidate(f"track-{index}", source=Source.SIMILAR_TRACK, similarity=1)
        for index in range(10)
    ]

    selection = select_tracks(score_candidates(tracks))

    assert selection.tracks == ()
    assert selection.eligible == 10
    assert not selection.publishable


def test_artist_cap_is_two_across_the_playlist():
    repeated = [
        _candidate(
            f"same-{index}",
            source=Source.SIMILAR_TRACK,
            similarity=10 - index,
            artist="Radiohead" if index < 2 else "RADIOHEAD",
        )
        for index in range(4)
    ]
    others = [
        _candidate(f"other-{index}", source=Source.SIMILAR_TRACK, similarity=0.1)
        for index in range(16)
    ]

    selection = select_tracks(score_candidates([*repeated, *others]))
    kept = [
        item.candidate.spotify_track_id
        for item in selection.tracks
        if item.candidate.artist.casefold().startswith("radiohead")
    ]

    assert selection.publishable
    assert kept == ["same-0", "same-1"]


def test_seed_cap_is_three():
    seeded = [
        _candidate(
            f"seeded-{index}",
            source=Source.SIMILAR_TRACK,
            similarity=10 - index,
            seed_id="shared",
        )
        for index in range(5)
    ]
    others = [
        _candidate(f"other-{index}", source=Source.SIMILAR_TRACK, similarity=0.1)
        for index in range(16)
    ]

    selection = select_tracks(score_candidates([*seeded, *others]))
    kept = [
        item.candidate.spotify_track_id
        for item in selection.tracks
        if item.candidate.seed_id == "shared"
    ]

    assert kept == ["seeded-0", "seeded-1", "seeded-2"]
