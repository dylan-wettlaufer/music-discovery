import pytest
from pydantic import ValidationError

from music_discovery.config import SOFT_EXCLUDE_MONTHS, Settings
from music_discovery.pipeline.types import ScoreWeights, SelectionCaps


def test_locked_defaults():
    assert SOFT_EXCLUDE_MONTHS == 6
    weights = ScoreWeights()
    assert (weights.similarity, weights.genre_overlap, weights.recency) == (0.55, 0.30, 0.15)
    caps = SelectionCaps()
    assert caps.playlist_size == 25
    assert caps.min_playlist_size == 15
    assert (caps.quota_similar_artist, caps.quota_similar_track, caps.quota_deep_cut) == (
        10,
        10,
        5,
    )
    assert caps.max_per_artist == 2
    assert caps.max_per_seed == 3


def test_settings_follow_the_locked_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in (
        "SCORE_WEIGHT_SIMILARITY",
        "SCORE_WEIGHT_GENRE_OVERLAP",
        "SCORE_WEIGHT_RECENCY",
        "PLAYLIST_SIZE",
        "MIN_PLAYLIST_SIZE",
        "QUOTA_SIMILAR_ARTIST",
        "QUOTA_SIMILAR_TRACK",
        "QUOTA_DEEP_CUT",
        "MAX_TRACKS_PER_ARTIST",
        "MAX_TRACKS_PER_SEED",
        "TIMEZONE",
        "POLL_INTERVAL_HOURS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.score_weights() == ScoreWeights()
    assert settings.selection_caps() == SelectionCaps()
    assert settings.timezone == "America/New_York"
    assert settings.poll_interval_hours == 3


def test_poll_interval_stays_within_two_to_three_hours():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, poll_interval_hours=24)
