"""Score surviving candidates and fill a playlist by source quota."""

from collections.abc import Sequence

from music_discovery.normalize import normalize
from music_discovery.pipeline.types import (
    Candidate,
    ScoreComponents,
    ScoreWeights,
    ScoredCandidate,
    Selection,
    SelectionCaps,
    Source,
)

_QUOTAS = (
    (Source.SIMILAR_ARTIST, "quota_similar_artist"),
    (Source.SIMILAR_TRACK, "quota_similar_track"),
    (Source.DEEP_CUT, "quota_deep_cut"),
)


def inverse_popularity(popularity: int | None) -> float:
    """Map Spotify popularity (0–100, higher is more famous) onto 0–1, inverted.

    Deep cuts have no Last.fm match. Obscurity is their similarity signal.
    """
    if popularity is None:
        return 0.0
    clamped = min(100, max(0, popularity))
    return 1.0 - (clamped / 100.0)


def score_candidates(
    candidates: Sequence[Candidate],
    weights: ScoreWeights | None = None,
) -> list[ScoredCandidate]:
    """Normalize each source onto 0–1, then apply the configured weights.

    ``track.getSimilar`` matches are an unbounded float. Artist matches are
    already 0–1. Both are rescaled inside the run before they are compared.
    Deep cuts use inverse popularity in place of a Last.fm match.
    """
    weights = weights or ScoreWeights()
    raw = [_source_signal(candidate) for candidate in candidates]
    normalized = _normalize_per_source(candidates, raw)
    scored: list[ScoredCandidate] = []
    for candidate, similarity in zip(candidates, normalized, strict=True):
        components = ScoreComponents(
            similarity=similarity,
            genre_overlap=candidate.genre_overlap,
            recency=candidate.recency,
        )
        total = (
            weights.similarity * components.similarity
            + weights.genre_overlap * components.genre_overlap
            + weights.recency * components.recency
        )
        scored.append(ScoredCandidate(candidate=candidate, score=total, components=components))
    return scored


def select_tracks(
    scored: Sequence[ScoredCandidate],
    caps: SelectionCaps | None = None,
) -> Selection:
    """Fill source quotas, then backfill. Refuse a playlist shorter than the minimum.

    Caps are global: two tracks per artist and three descended from the same
    seed, across every source. Order is score within each quota block, then
    backfill by score.
    """
    caps = caps or SelectionCaps()
    ranked = sorted(scored, key=lambda item: item.score, reverse=True)
    picked: list[ScoredCandidate] = []
    picked_ids: set[str] = set()
    artist_counts: dict[str, int] = {}
    seed_counts: dict[str, int] = {}

    def can_take(item: ScoredCandidate) -> bool:
        artist = normalize(item.candidate.artist)
        if artist_counts.get(artist, 0) >= caps.max_per_artist:
            return False
        seed_id = item.candidate.seed_id
        if seed_id is not None and seed_counts.get(seed_id, 0) >= caps.max_per_seed:
            return False
        return True

    def take(item: ScoredCandidate) -> None:
        picked.append(item)
        picked_ids.add(item.candidate.spotify_track_id)
        artist = normalize(item.candidate.artist)
        artist_counts[artist] = artist_counts.get(artist, 0) + 1
        seed_id = item.candidate.seed_id
        if seed_id is not None:
            seed_counts[seed_id] = seed_counts.get(seed_id, 0) + 1

    for source, quota_attr in _QUOTAS:
        quota = getattr(caps, quota_attr)
        taken = 0
        for item in ranked:
            if len(picked) >= caps.playlist_size or taken >= quota:
                break
            if item.candidate.source is not source or item.candidate.spotify_track_id in picked_ids:
                continue
            if can_take(item):
                take(item)
                taken += 1

    for item in ranked:
        if len(picked) >= caps.playlist_size:
            break
        if item.candidate.spotify_track_id in picked_ids:
            continue
        if can_take(item):
            take(item)

    eligible = len(picked)
    if eligible < caps.min_playlist_size:
        return Selection(tracks=(), eligible=eligible)
    return Selection(tracks=tuple(picked), eligible=eligible)


def _source_signal(candidate: Candidate) -> float:
    if candidate.source is Source.DEEP_CUT:
        return inverse_popularity(candidate.popularity)
    return candidate.similarity


def _normalize_per_source(candidates: Sequence[Candidate], raw: Sequence[float]) -> list[float]:
    grouped: dict[Source, list[float]] = {source: [] for source in Source}
    for candidate, value in zip(candidates, raw, strict=True):
        grouped[candidate.source].append(value)
    scaled = {source: _min_max(values) for source, values in grouped.items()}
    cursors = {source: 0 for source in Source}
    normalized: list[float] = []
    for candidate in candidates:
        index = cursors[candidate.source]
        cursors[candidate.source] += 1
        normalized.append(scaled[candidate.source][index])
    return normalized


def _min_max(values: list[float]) -> list[float]:
    if not values:
        return []
    low = min(values)
    high = max(values)
    if high == low:
        return [1.0 for _ in values]
    return [(value - low) / (high - low) for value in values]
