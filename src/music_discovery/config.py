"""Environment and the locked scoring, quota, and schedule defaults."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from music_discovery.pipeline.types import ScoreWeights, SelectionCaps

# A recommended track that was never played can return after this window.
# A later play promotes it to a lifetime hard exclude.
SOFT_EXCLUDE_MONTHS = 6


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = "http://127.0.0.1:8888/callback"

    lastfm_api_key: str = ""
    lastfm_requests_per_second: float = 4.0
    lastfm_cache_ttl_days: int = 30

    database_url: str = "postgresql+psycopg://music:music@localhost:5432/music_discovery"
    token_encryption_key: str = ""

    timezone: str = "America/New_York"
    poll_interval_hours: int = Field(default=3, ge=2, le=3)
    saved_tracks_hour: int = Field(default=6, ge=0, le=23)
    weekly_day_of_week: str = "sun"
    weekly_hour: int = Field(default=23, ge=0, le=23)
    weekly_minute: int = Field(default=0, ge=0, le=59)

    score_weight_similarity: float = ScoreWeights().similarity
    score_weight_genre_overlap: float = ScoreWeights().genre_overlap
    score_weight_recency: float = ScoreWeights().recency

    playlist_size: int = SelectionCaps().playlist_size
    min_playlist_size: int = SelectionCaps().min_playlist_size
    quota_similar_artist: int = SelectionCaps().quota_similar_artist
    quota_similar_track: int = SelectionCaps().quota_similar_track
    quota_deep_cut: int = SelectionCaps().quota_deep_cut
    max_tracks_per_artist: int = SelectionCaps().max_per_artist
    max_tracks_per_seed: int = SelectionCaps().max_per_seed

    def score_weights(self) -> ScoreWeights:
        return ScoreWeights(
            similarity=self.score_weight_similarity,
            genre_overlap=self.score_weight_genre_overlap,
            recency=self.score_weight_recency,
        )

    def selection_caps(self) -> SelectionCaps:
        return SelectionCaps(
            playlist_size=self.playlist_size,
            min_playlist_size=self.min_playlist_size,
            quota_similar_artist=self.quota_similar_artist,
            quota_similar_track=self.quota_similar_track,
            quota_deep_cut=self.quota_deep_cut,
            max_per_artist=self.max_tracks_per_artist,
            max_per_seed=self.max_tracks_per_seed,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
