"""Dedupe candidates and apply lifetime and soft excludes."""

from collections.abc import Sequence

from music_discovery.pipeline.types import Candidate, Exclusions, FilterResult, Source


def filter_candidates(
    candidates: Sequence[Candidate],
    exclusions: Exclusions | None = None,
) -> FilterResult:
    """Collapse duplicates, then drop anything the user already knows or was just offered.

    The same Spotify id, or the same normalized artist and title, is one song.
    The survivor keeps the highest similarity and every source that produced it.
    Hard excludes are lifetime (plays and saved tracks). Soft excludes are the
    caller's already-windowed set of recent unplayed recommendations.
    """
    exclusions = exclusions or Exclusions()
    unique = _dedupe(candidates)
    kept: list[Candidate] = []
    hard_excluded = 0
    soft_excluded = 0
    for candidate in unique:
        if _matches(candidate, exclusions.track_ids, exclusions.normalized_keys):
            hard_excluded += 1
            continue
        if _matches(candidate, exclusions.soft_track_ids, exclusions.soft_normalized_keys):
            soft_excluded += 1
            continue
        kept.append(candidate)
    return FilterResult(
        candidates=tuple(kept),
        hard_excluded=hard_excluded,
        soft_excluded=soft_excluded,
    )


def _matches(candidate: Candidate, track_ids: frozenset[str], keys: frozenset[str]) -> bool:
    return candidate.spotify_track_id in track_ids or candidate.normalized_key in keys


def _dedupe(candidates: Sequence[Candidate]) -> list[Candidate]:
    parent = list(range(len(candidates)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    by_id: dict[str, int] = {}
    by_key: dict[str, int] = {}
    for index, candidate in enumerate(candidates):
        previous_id = by_id.get(candidate.spotify_track_id)
        if previous_id is not None:
            union(index, previous_id)
        by_id[candidate.spotify_track_id] = index

        previous_key = by_key.get(candidate.normalized_key)
        if previous_key is not None:
            union(index, previous_key)
        by_key[candidate.normalized_key] = index

    groups: dict[int, list[Candidate]] = {}
    for index, candidate in enumerate(candidates):
        groups.setdefault(find(index), []).append(candidate)

    merged: list[Candidate] = []
    for group in groups.values():
        winner = max(group, key=lambda item: item.similarity)
        sources: set[Source] = set()
        for item in group:
            sources.update(item.all_sources())
        merged.append(
            Candidate(
                spotify_track_id=winner.spotify_track_id,
                artist=winner.artist,
                title=winner.title,
                source=winner.source,
                similarity=winner.similarity,
                genre_overlap=winner.genre_overlap,
                recency=winner.recency,
                popularity=winner.popularity,
                seed_id=winner.seed_id,
                sources=frozenset(sources),
            )
        )
    return merged
