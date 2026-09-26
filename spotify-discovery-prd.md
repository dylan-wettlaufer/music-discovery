# PRD: Spotify Never-Repeat Discovery Playlist

## Problem

Spotify's recommendation algorithms (Discover Weekly, Release Radar) are optimized for engagement and retention, not novelty. Over time, recommendations converge on music the user has already heard or that closely resembles their existing listening habits. There is no built-in guarantee that a recommended track has never been played before — a strict novelty guarantee that users have publicly and repeatedly requested from Spotify without it being shipped.

## Goal

Build a tool that generates a new, automatically-created Spotify playlist every week, made up entirely of tracks the user has never listened to before — enforced as a hard rule, not a soft preference.

## Target user

v1: the builder, as the sole user. This is a personal tool first; broader usability is a stretch goal, not a v1 requirement.

## Success criteria

- A new playlist appears in the user's Spotify account every week, automatically, with no manual steps required after initial setup.
- Zero tracks in any generated playlist have appeared in the user's prior listening history.
- The system runs unattended for multiple consecutive weeks without breaking.

## V1 scope

### In scope
- Spotify OAuth login (one-time setup)
- Daily background job that polls `recently-played` and appends new plays to a persistent history store (required because Spotify's API only exposes a shallow rolling window of recent plays, not full history)
- Weekly background job (Sunday night) that:
  - Gathers seed data from the user's top artists, top tracks, and saved tracks
  - Generates candidate tracks from three sources: similar artists (via Last.fm `artist.getSimilar` → `artist.getTopTracks`), similar tracks (via Last.fm `track.getSimilar`), and deep cuts from artists the user already likes (via Spotify `artists/{id}/albums`)
  - Resolves Last.fm artist/track names to Spotify URIs via Spotify search
  - Deduplicates the candidate pool
  - Filters out any track already in the user's listening history (hard exclude)
  - Filters out any track recommended within the last N months that the user did not play (soft exclude, time-boxed, not permanent)
  - Scores remaining candidates using a weighted combination of similarity score, genre overlap, and optionally release recency
  - Selects the top ~25 tracks
  - Creates a new playlist in the user's Spotify account and populates it
- Passive delivery — the user simply finds the playlist in their Spotify library; no separate app UI required for v1


## Open decisions

- **Exclusion window for history**: lifetime (strict, but candidate pool shrinks over time) vs. rolling window (e.g. 6 months). Leaning toward lifetime for v1 given the project's core promise, revisit if the candidate pool runs dry.
- **Notification method**: fully passive (playlist just appears) vs. an active nudge (email/push). Leaning toward passive for v1 to keep scope minimal.

## System flow

1. User connects Spotify via OAuth (one-time).
2. Daily job polls `recently-played`, appends new plays to the history table.
3. Weekly cron job (Sunday night) runs the recommendation pipeline (see below) and creates a new playlist.
4. User discovers the new playlist in Spotify the following week.
5. Any tracks played from that playlist are captured by the next daily poll, closing the loop so they're never recommended again.

## Recommendation pipeline

1. **Gather seeds** — pull user's top artists, top tracks, and saved tracks from Spotify.
2. **Generate candidates** from three parallel sources:
   - Similar artists → their top tracks (Last.fm, two-step chain)
   - Similar tracks (Last.fm, single call)
   - Deep cuts from already-liked artists' back catalogs (Spotify, no external dependency)
3. **Resolve to Spotify URIs** — Last.fm returns artist/track names, not playable identifiers; each candidate must be matched to a Spotify track via search. Expect and tolerate a non-zero no-match rate.
4. **Filter candidates** — deduplicate across sources, hard-exclude anything in listening history, soft-exclude anything recommended recently but unplayed.
5. **Score and select top 25** — weighted combination of similarity score, genre overlap, and (optionally) recency.
6. **Create Spotify playlist** — via the Playlists API, named and dated (e.g. "Fresh — Nov 3").

## Technical constraints

- Spotify deprecated `/recommendations`, `/audio-features`, `/audio-analysis`, `/related-artists`, and several browse endpoints for apps created after November 27, 2024. The pipeline is designed around this — it does not depend on any of these dead endpoints.
- `preview_url` is largely unpopulated in current API responses; any future feature relying on 30-second previews (e.g. a swipe-discovery feed) needs to be validated against this constraint before being built.
- Last.fm's API has a rate limit (approx. 5 requests/second); the weekly job should throttle accordingly.
- Full listening history is not available retroactively from Spotify — the exclusion pool only grows from whichever date the user connects their account onward.

## Tech stack (proposed)

- **Language**: Python
- **Storage**: SQLite with a vector-friendly or simple relational schema (no need for hosted infra at single-user scale)
- **Scheduling**: cron job or scheduled cloud function
- **APIs**: Spotify Web API (OAuth, search, playlists, top items, recently-played, albums), Last.fm API (artist/track similarity, top tracks)

## Future / v2+ ideas

- Swipe-style discovery feed (subject to preview-URL availability) with liked songs feeding into that week's playlist
- LLM-based reranking or curation of the candidate list, with reasoning/explanations for top picks (kept clearly separate from candidate sourcing to avoid hallucinated recommendations)
- Adjustable novelty distance (near-adjacent discovery vs. deliberately distant discovery)
- Multi-user / hosted version

## Portfolio framing

This project is intended as a resume/portfolio piece demonstrating: OAuth integration, scheduled background jobs, data modeling over a growing dataset, multi-API integration and reconciliation (Last.fm ↔ Spotify identifier resolution), and a content-based recommendation pipeline built without relying on a deprecated first-party API.