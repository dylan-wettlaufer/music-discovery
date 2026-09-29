"""Postgres schema.

One user. Plays and saved tracks are lifetime excludes, matched on Spotify
track id and on ``tracks.normalized_key``. ``recommendation_runs`` is unique
per user per week so a retry cannot create a second playlist.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from music_discovery.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    spotify_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    country: Mapped[str | None] = mapped_column(String(2))
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_encrypted: Mapped[str | None] = mapped_column(Text)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recently_played_cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (UniqueConstraint("spotify_user_id", name="uq_users_spotify_user_id"),)


class Track(Base):
    """One row per Spotify track id. ``normalized_key`` catches a remaster with a new id."""

    __tablename__ = "tracks"

    spotify_track_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    primary_artist: Mapped[str] = mapped_column(String(512), nullable=False)
    artist_ids: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    album_name: Mapped[str | None] = mapped_column(String(512))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    release_date: Mapped[str | None] = mapped_column(String(32))
    popularity: Mapped[int | None] = mapped_column(Integer)
    normalized_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (Index("ix_tracks_normalized_key", "normalized_key"),)


class PlayEvent(Base):
    __tablename__ = "play_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_play_events_user_id_users", ondelete="CASCADE"),
        nullable=False,
    )
    spotify_track_id: Mapped[str] = mapped_column(
        ForeignKey(
            "tracks.spotify_track_id",
            name="fk_play_events_spotify_track_id_tracks",
        ),
        nullable=False,
    )
    played_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    context_uri: Mapped[str | None] = mapped_column(String(512))

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "spotify_track_id",
            "played_at",
            name="uq_play_events_user_track_played_at",
        ),
        Index("ix_play_events_user_track", "user_id", "spotify_track_id"),
    )


class SavedTrack(Base):
    """Library membership. These tracks are seeds and lifetime hard excludes."""

    __tablename__ = "saved_tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_saved_tracks_user_id_users", ondelete="CASCADE"),
        nullable=False,
    )
    spotify_track_id: Mapped[str] = mapped_column(
        ForeignKey(
            "tracks.spotify_track_id",
            name="fk_saved_tracks_spotify_track_id_tracks",
        ),
        nullable=False,
    )
    added_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("user_id", "spotify_track_id", name="uq_saved_tracks_user_track"),
    )


class RecommendationRun(Base):
    __tablename__ = "recommendation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey(
            "users.id",
            name="fk_recommendation_runs_user_id_users",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    spotify_playlist_id: Mapped[str | None] = mapped_column(String(64))
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("user_id", "week_start", name="uq_recommendation_runs_user_week"),
    )


class RecommendationItem(Base):
    """A track placed on a generated playlist. This table is the soft-exclude list."""

    __tablename__ = "recommendation_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey(
            "recommendation_runs.id",
            name="fk_recommendation_items_run_id_recommendation_runs",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    spotify_track_id: Mapped[str] = mapped_column(
        ForeignKey(
            "tracks.spotify_track_id",
            name="fk_recommendation_items_spotify_track_id_tracks",
        ),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_score: Mapped[float] = mapped_column(Float, nullable=False)
    normalized_score: Mapped[float] = mapped_column(Float, nullable=False)
    components: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        Index("ix_recommendation_items_run_id", "run_id"),
        Index("ix_recommendation_items_spotify_track_id", "spotify_track_id"),
    )


class Resolution(Base):
    """Cache from a normalized artist and title to a Spotify id, or a recorded miss."""

    __tablename__ = "resolutions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    normalized_artist: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_title: Mapped[str] = mapped_column(String(512), nullable=False)
    spotify_track_id: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "normalized_artist",
            "normalized_title",
            name="uq_resolutions_artist_title",
        ),
    )


class LastfmCache(Base):
    __tablename__ = "lastfm_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    params_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("method", "params_hash", name="uq_lastfm_cache_method_params"),
    )


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_name: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    gap_warning: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (Index("ix_job_runs_started_at", "started_at"),)
