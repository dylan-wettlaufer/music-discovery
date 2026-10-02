# Scoring and selection

`score_candidates` and `select_tracks` in `src/music_discovery/pipeline/score.py` are pure functions. The weekly job calls them with the weights and caps from `Settings` in `src/music_discovery/config.py`.

## Score

Each candidate has a source signal:

- Similar-artist and similar-track candidates use `similarity`.
- Deep cuts use inverse popularity: Spotify popularity 0–100 mapped to 1–0. Missing popularity is 0.

Signals are min-max scaled inside each source for that run, so an unbounded Last.fm track match can be compared with an artist match that is already 0–1. If every value in a source is equal, each of them scales to 1.

The total is a weighted sum:

| Component | Default weight |
|---|---|
| Similarity (the scaled source signal) | 0.55 |
| Genre overlap | 0.30 |
| Recency | 0.15 |

Genre overlap and recency are already on the candidate. Weights cannot be negative.

## Selection

Default playlist size is 25, with a minimum of 15. Quotas, which must sum to the playlist size:

| Source | Slots |
|---|---|
| Similar artist | 10 |
| Similar track | 10 |
| Deep cut | 5 |

Selection walks score order inside each quota, then backfills remaining slots by score. Across the whole playlist, at most two tracks share a normalized artist name and at most three share a seed id. A seed id of none does not count toward the seed cap.

If fewer than the minimum survive the quotas and caps, `tracks` is empty and `publishable` is false. `eligible` is how many passed the caps before that minimum was applied.
