"""In-memory candidate, exclusion, and scoring types.

Filter and score are pure. Callers load history, saved tracks, and the
six-month recommendation window from Postgres and pass them in.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from music_discovery.normalize import normalized_key


class Source(StrEnum):
    SIMILAR_ARTIST = "similar_artist"
    SIMILAR_TRACK = "similar_track"
    DEEP_CUT = "deep_cut"


@dataclass(frozen=True)
class Candidate:
    spotify_track_id: str
    artist: str
    title: str
    source: Source
    similarity: float
    genre_overlap: float = 0.0
    recency: float = 0.0
    popularity: int | None = None
    seed_id: str | None = None
    sources: frozenset[Source] = field(default_factory=frozenset)

    @property
    def normalized_key(self) -> str:
        return normalized_key(self.artist, self.title)

    def all_sources(self) -> frozenset[Source]:
        if self.sources:
            return self.sources
        return frozenset((self.source,))


@dataclass(frozen=True)
class Exclusions:
    """Lifetime hard excludes, plus the current soft-exclude window.

    ``normalized_keys`` must be built with :func:`normalized_key`. Soft keys
    are recommended-but-unplayed tracks from the last six months.
    """

    track_ids: frozenset[str] = field(default_factory=frozenset)
    normalized_keys: frozenset[str] = field(default_factory=frozenset)
    soft_track_ids: frozenset[str] = field(default_factory=frozenset)
    soft_normalized_keys: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class FilterResult:
    candidates: tuple[Candidate, ...]
    hard_excluded: int
    soft_excluded: int


@dataclass(frozen=True)
class ScoreWeights:
    similarity: float = 0.55
    genre_overlap: float = 0.30
    recency: float = 0.15

    def __post_init__(self) -> None:
        for name in ("similarity", "genre_overlap", "recency"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} weight cannot be negative")


@dataclass(frozen=True)
class ScoreComponents:
    similarity: float
    genre_overlap: float
    recency: float


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: Candidate
    score: float
    components: ScoreComponents


@dataclass(frozen=True)
class SelectionCaps:
    playlist_size: int = 25
    min_playlist_size: int = 15
    quota_similar_artist: int = 10
    quota_similar_track: int = 10
    quota_deep_cut: int = 5
    max_per_artist: int = 2
    max_per_seed: int = 3

    def __post_init__(self) -> None:
        quota = self.quota_similar_artist + self.quota_similar_track + self.quota_deep_cut
        if quota != self.playlist_size:
            raise ValueError(
                f"source quotas sum to {quota}, expected playlist size {self.playlist_size}"
            )
        if self.min_playlist_size > self.playlist_size:
            raise ValueError("minimum playlist size cannot exceed playlist size")
        if self.min_playlist_size < 1:
            raise ValueError("minimum playlist size must be at least 1")


@dataclass(frozen=True)
class Selection:
    """Tracks to publish.

    ``tracks`` is empty when fewer than the minimum survive, so a short list
    is never published. ``eligible`` counts how many passed quotas and caps
    before that minimum was applied.
    """

    tracks: tuple[ScoredCandidate, ...]
    eligible: int

    @property
    def publishable(self) -> bool:
        return len(self.tracks) > 0
