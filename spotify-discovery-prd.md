# PRD: Spotify Never-Repeat Discovery Playlist

## Problem

Spotify's recommendation algorithms (Discover Weekly, Release Radar) are optimized for engagement and retention, not novelty. Over time, recommendations converge on music the user has already heard or that closely resembles their existing listening habits. There is no built-in guarantee that a recommended track has never been played before — a strict novelty guarantee that users have publicly and repeatedly requested from Spotify without it being shipped.

## Goal

Build a tool that generates a new, automatically-created Spotify playlist every week, made up entirely of tracks the user has never listened to before — enforced as a hard rule, not a soft preference.

## Target user

v1: the builder, as the sole user. This is a personal tool first; broader usability is a stretch goal, not a v1 requirement.

## Success criteria

- A new playlist appears in the user's Spotify account every week, automatically, with no manual steps required after initial setup.
- Zero tracks in any generated playlist match the user's observed listening history or saved library, by Spotify track id or by normalized artist + title (so remasters and deluxe copies of a heard song are excluded too).
- The system runs unattended for multiple consecutive weeks without breaking. A week with fewer than 15 eligible tracks publishes nothing rather than a thin playlist.

## V1 scope

### In scope
- Spotify OAuth login (one-time setup). The app stays in Spotify development mode; the user's account is allowlisted.
- Background poll every two to three hours of `recently-played`, appending new plays to a persistent history store. Spotify only exposes the last 50 plays, so a daily poll drops history on a heavy listening day. Each poll records a gap warning when that window has already overflowed.
- Daily ingest of saved tracks. They are seeds, and they are also a hard exclude, because library tracks are known music even when they never appear in `recently-played`.
- Weekly background job (Sunday 23:00 `America/New_York`) that:
  - Gathers seed data from the user's top artists, top tracks, and a sample of saved tracks (about 30 artists and 40 tracks)
  - Generates candidate tracks from three sources: similar artists (via Last.fm `artist.getSimilar` → `artist.getTopTracks`), similar tracks (via Last.fm `track.getSimilar`), and deep cuts from artists the user already likes (via Spotify `artists/{id}/albums`)
  - Resolves Last.fm artist/track names to Spotify URIs via Spotify search, and caches hits and misses
  - Deduplicates the candidate pool on Spotify id and on normalized artist + title
  - Hard-excludes, for life, any track already in observed listening history or in saved tracks
  - Soft-excludes any track recommended within the last 6 months that the user did not play
  - Scores remaining candidates with similarity 0.55, genre overlap 0.30, and release recency 0.15
  - Selects about 25 tracks with source quotas (about 10 similar-artist, 10 similar-track, 5 deep-cut), at most 2 per artist and 3 per seed
  - Creates a new private playlist in the user's Spotify account and populates it, unless fewer than 15 tracks survive
- Passive delivery — the user finds the playlist in their Spotify library. No app UI and no email or push in v1.

Implementation of the pipeline, schema, failure behavior, and build order is in [technical-deep-dive.md](technical-deep-dive.md).

## Decisions

- **History exclusion is lifetime.** Observed plays and saved tracks stay excluded forever. Revisit only if more than one weekly run falls under 15 eligible tracks.
- **Soft exclude window is 6 months.** A track that was recommended and never played can return after that. A later play promotes it to a lifetime hard exclude.
- **Delivery is passive.** The playlist appearing is the notification.
- **History poll is every 2–3 hours**, not daily, because `recently-played` only retains the last 50 tracks.
- **No swipe feed and no LLM in v1.** Preview URLs are unavailable to a new app, and a model must not invent tracks. A later reranker may only reorder ids this pipeline already resolved.

## System flow

1. User connects Spotify via OAuth (one-time).
2. A job every 2–3 hours polls `recently-played` and appends new plays. Saved tracks are refreshed daily and marked excluded.
3. Weekly job (Sunday 23:00 `America/New_York`) runs the recommendation pipeline and creates a new private playlist, or records a failed run if the eligible pool is under 15.
4. User discovers the new playlist in Spotify the following week.
5. Any tracks played from that playlist (more than 30 seconds) are captured by the next poll, closing the loop so they are never recommended again. Skips stay out of history and remain soft-excluded for 6 months.

## Recommendation pipeline

1. **Gather seeds** — top artists and top tracks (`medium_term` and `long_term`, with `short_term` used lightly) plus a sample of saved tracks.
2. **Generate candidates** from three sources:
   - Similar artists → their top tracks (Last.fm, two-step chain). Similarity is the artist `match` (0–1) times a rank decay.
   - Similar tracks (Last.fm). `track.getSimilar` `match` is an unbounded float and must be normalized before it is compared with artist matches.
   - Deep cuts from already-liked artists' album tracks (Spotify). Hits are dropped; these candidates use inverse popularity in place of a Last.fm match.
3. **Resolve to Spotify URIs** — Last.fm returns artist/track names, not playable identifiers. Search with the user's market and accept only a high-confidence match. Cache hits and misses. Expect and tolerate a non-zero no-match rate.
4. **Filter candidates** — dedupe on Spotify id and normalized artist + title, hard-exclude history and saved tracks for life, soft-exclude anything recommended in the last 6 months but unplayed.
5. **Score and select ~25** — weighted similarity, genre overlap, and recency, then fill source quotas instead of taking a pure top-25. Do not publish if fewer than 15 tracks remain.
6. **Create Spotify playlist** — private, named and dated (e.g. "Fresh — Nov 3"). One run per user per week; a retry must not create a second playlist.

## Technical constraints

- Spotify removed `/recommendations`, `/audio-features`, `/audio-analysis`, `/related-artists`, and several browse endpoints for apps created after November 27, 2024, and for development-mode apps. Extended quota mode has only accepted organization applications since May 2025. The pipeline does not call any of those endpoints. `preview_url` is not available for a new app; a swipe-discovery feed has to be revalidated before it is built.
- `recently-played` returns at most the last 50 tracks played for more than 30 seconds. `before` and `after` only slice that window. Full listening history is not available retroactively — the exclusion pool grows from the connect date onward, plus saved tracks.
- Last.fm's API allows about 5 requests/second. The weekly job throttles to about 4/second, backs off on error 29, and caches responses for about 30 days.
- Spotify development-mode rate limits are easy to exhaust with uncached search. Resolutions are persisted so a retry and the following week do not repeat the same searches.

## Tech stack

- **Language**: Python 3.12 (`httpx`, Pydantic, Typer)
- **Storage**: Postgres 16 (SQLAlchemy 2, Alembic)
- **Scheduling**: APScheduler in one long-running process, timezone `America/New_York` (Docker Compose for the app and Postgres)
- **APIs**: Spotify Web API (OAuth, search, playlists, top items, recently-played, saved tracks, albums) and Last.fm API (artist/track similarity, top tracks)
- **Auth**: authorization-code flow with a localhost redirect, encrypted refresh token at rest. Scopes: `user-read-private`, `user-read-recently-played`, `user-top-read`, `user-library-read`, `playlist-modify-private`

## Future / v2+ ideas

- Swipe-style discovery feed (subject to preview-URL availability) with liked songs feeding into that week's playlist
- LLM-based reranking of an already-resolved candidate list, with reasoning for top picks. The model must not be a source of track names.
- Adjustable novelty distance (near-adjacent discovery vs. deliberately distant discovery)
- Multi-user / hosted version

## Portfolio framing

This project is intended as a resume/portfolio piece demonstrating: OAuth with rotating refresh tokens, scheduled background jobs that detect when a shallow API window dropped data, data modeling over a growing exclude set, multi-API reconciliation (Last.fm names to Spotify ids), and a content-based recommendation pipeline that does not depend on Spotify's deprecated recommendation endpoints.
