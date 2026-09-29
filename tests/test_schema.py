from sqlalchemy import UniqueConstraint

import music_discovery.models  # noqa: F401
from music_discovery.db import Base


def test_schema_has_the_locked_tables():
    assert set(Base.metadata.tables) == {
        "users",
        "tracks",
        "play_events",
        "saved_tracks",
        "recommendation_runs",
        "recommendation_items",
        "resolutions",
        "lastfm_cache",
        "job_runs",
    }


def _unique_columns(table_name: str) -> set[frozenset[str]]:
    table = Base.metadata.tables[table_name]
    return {
        frozenset(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_idempotency_keys():
    assert frozenset({"spotify_user_id"}) in _unique_columns("users")
    assert frozenset({"user_id", "spotify_track_id", "played_at"}) in _unique_columns("play_events")
    assert frozenset({"user_id", "spotify_track_id"}) in _unique_columns("saved_tracks")
    assert frozenset({"user_id", "week_start"}) in _unique_columns("recommendation_runs")
    assert frozenset({"normalized_artist", "normalized_title"}) in _unique_columns("resolutions")
    assert frozenset({"method", "params_hash"}) in _unique_columns("lastfm_cache")
