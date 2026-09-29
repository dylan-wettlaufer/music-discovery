from music_discovery.normalize import normalized_key
from music_discovery.pipeline.filter import filter_candidates
from music_discovery.pipeline.score import score_candidates, select_tracks
from music_discovery.pipeline.types import Candidate, Exclusions, Source


def _track(
    track_id: str,
    *,
    artist: str,
    title: str,
    source: Source = Source.SIMILAR_TRACK,
    similarity: float = 0.5,
    seed_id: str | None = None,
    popularity: int | None = None,
) -> Candidate:
    return Candidate(
        spotify_track_id=track_id,
        artist=artist,
        title=title,
        source=source,
        similarity=similarity,
        genre_overlap=0.2,
        recency=0.2,
        popularity=popularity,
        seed_id=seed_id if seed_id is not None else track_id,
    )


def _fresh(count: int) -> list[Candidate]:
    return [
        _track(
            f"fresh-{index}",
            artist=f"Artist {index}",
            title=f"Title {index}",
            similarity=0.5,
        )
        for index in range(count)
    ]


def _selected_ids(candidates: list[Candidate], exclusions: Exclusions) -> set[str]:
    filtered = filter_candidates(candidates, exclusions)
    selection = select_tracks(score_candidates(filtered.candidates))
    assert selection.publishable
    return {item.candidate.spotify_track_id for item in selection.tracks}


def test_dedupe_keeps_the_highest_similarity_and_every_source():
    low = _track(
        "same",
        artist="Radiohead",
        title="Karma Police",
        source=Source.SIMILAR_TRACK,
        similarity=0.2,
    )
    high = _track(
        "same",
        artist="Radiohead",
        title="Karma Police",
        source=Source.SIMILAR_ARTIST,
        similarity=0.9,
    )

    result = filter_candidates([low, high])

    assert len(result.candidates) == 1
    assert result.candidates[0].spotify_track_id == "same"
    assert result.candidates[0].similarity == 0.9
    assert result.candidates[0].source is Source.SIMILAR_ARTIST
    assert result.candidates[0].all_sources() == frozenset(
        {Source.SIMILAR_ARTIST, Source.SIMILAR_TRACK}
    )


def test_dedupe_collapses_a_remaster_onto_one_candidate():
    original = _track(
        "original",
        artist="Radiohead",
        title="Karma Police",
        source=Source.SIMILAR_TRACK,
        similarity=0.4,
    )
    remaster = _track(
        "remaster",
        artist="Radiohead",
        title="Karma Police (Remastered)",
        source=Source.DEEP_CUT,
        similarity=0.8,
        popularity=12,
    )

    result = filter_candidates([original, remaster])

    assert len(result.candidates) == 1
    assert result.candidates[0].spotify_track_id == "remaster"
    assert result.candidates[0].all_sources() == frozenset({Source.SIMILAR_TRACK, Source.DEEP_CUT})


def test_hard_exclude_by_spotify_id():
    heard = _track("heard", artist="Radiohead", title="Karma Police", similarity=1.0)
    fresh = _fresh(1)

    result = filter_candidates(
        [heard, *fresh],
        Exclusions(track_ids=frozenset({"heard"})),
    )

    assert [candidate.spotify_track_id for candidate in result.candidates] == ["fresh-0"]
    assert result.hard_excluded == 1
    assert result.soft_excluded == 0


def test_hard_exclude_by_normalized_artist_and_title():
    remaster = _track(
        "remaster",
        artist="Radiohead",
        title="Karma Police - 2011 Remaster",
        similarity=1.0,
    )

    result = filter_candidates(
        [remaster],
        Exclusions(normalized_keys=frozenset({normalized_key("Radiohead", "Karma Police")})),
    )

    assert result.candidates == ()
    assert result.hard_excluded == 1


def test_soft_exclude_drops_a_recent_unplayed_recommendation():
    offered = _track("offered", artist="Artist", title="Song")

    result = filter_candidates([offered], Exclusions(soft_track_ids=frozenset({"offered"})))

    assert result.candidates == ()
    assert result.soft_excluded == 1
    assert result.hard_excluded == 0


def test_known_spotify_id_is_absent_from_the_selected_set():
    heard = _track("heard", artist="Radiohead", title="Karma Police", similarity=1.0)
    selected = _selected_ids(
        [heard, *_fresh(16)],
        Exclusions(track_ids=frozenset({"heard"})),
    )

    assert "heard" not in selected
    assert len(selected) == 16


def test_known_song_under_another_spotify_id_is_absent_from_the_selected_set():
    remaster = _track(
        "remaster",
        artist="Radiohead",
        title="Karma Police (feat. Nobody) [Remastered]",
        similarity=1.0,
    )
    selected = _selected_ids(
        [remaster, *_fresh(16)],
        Exclusions(normalized_keys=frozenset({normalized_key("radiohead", "karma police")})),
    )

    assert "remaster" not in selected
    assert len(selected) == 16
