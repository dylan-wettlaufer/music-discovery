# Technical deep dive: Spotify Never-Repeat Discovery Playlist

This is a single-user background service that creates a Spotify playlist every week made only of tracks the user has not already played. The novelty rule is enforced in our own database, because Spotify will not enforce it and its recommendation endpoints are unavailable to a new app.

Implementation detail lives here. Product scope and locked decisions live in [spotify-discovery-prd.md](spotify-discovery-prd.md).

## What we are building

Spotify’s Discover Weekly optimizes for engagement, so it drifts back toward music the user already knows. This project does the opposite: once a week it writes a playlist of about 25 tracks into the user’s account, and it refuses to include anything already in their listening history.

Version 1 has no product UI. After a one-time OAuth connect, two unattended jobs do the work:

1. A poll of `GET /me/player/recently-played` every two to three hours that appends plays to Postgres.
2. A Sunday-night job that builds candidates, throws out anything the user has heard or was recently offered, scores what remains, and creates a dated playlist such as `Fresh — Nov 3`.

The success bar from the PRD is strict: the playlist shows up every week with no manual step, no selected track has appeared in stored history, and the loop survives multiple weeks without intervention.

## Constraints that decide the architecture

**Spotify will not recommend for you.** Apps registered after 27 November 2024, and development-mode apps, cannot use Related Artists, Recommendations, Audio Features, Audio Analysis, the browse/editorial playlist endpoints, or reliable 30-second preview URLs. Extended quota mode, which still has those endpoints for older apps, has only accepted organization applications since May 2025. A personal project stays in development mode and never calls those endpoints.

What remains, and what the pipeline uses:

| Need | Endpoint |
|---|---|
| Identity and market | `GET /me` |
| Plays | `GET /me/player/recently-played` |
| Seeds | `GET /me/top/artists`, `GET /me/top/tracks`, `GET /me/tracks` |
| Deep cuts | `GET /artists/{id}/albums`, `GET /albums/{id}/tracks`, batched `GET /tracks` |
| Genre overlap | batched `GET /artists` (`genres` is still on the artist object) |
| Name to playable id | `GET /search?type=track` |
| Delivery | `POST /users/{id}/playlists`, `POST /playlists/{id}/tracks` |

**History is a 50-track window, not an archive.** `recently-played` returns at most the last 50 tracks that were played for more than 30 seconds. `before` and `after` only slice that window; they do not page into older history. A daily poll is not enough. A few hours of listening can push more than 50 tracks through the window and those plays are gone forever. The history job runs every two to three hours, with gap detection when the window is already full of plays newer than the last cursor.

**History starts at connect time.** Spotify will not backfill. The exclusion set only contains plays observed after OAuth, plus saved tracks, which are treated as known music even when they never appear in `recently-played`.

**Last.fm supplies similarity, and its two scores are not on the same scale.** `artist.getSimilar` returns `match` from 0 to 1. `track.getSimilar` returns an unbounded float (a documented example is `10.95`). `artist.getTopTracks` returns playcount, not similarity. The scorer normalizes per source. Last.fm’s published guidance is about 5 requests per second per IP; error 29 means back off. Responses are cached, because similar-artist graphs barely change week to week.

**Development-mode quotas are undocumented and easy to trip.** Search is one HTTP call per candidate. Without a resolution cache, a weekly run can be hundreds of Spotify searches and will 429. Every external call needs throttle, retry, and persistence of both hits and misses.

## Runtime shape

One Python process, one Postgres database, no web app.

```
cron or APScheduler
        │
        ├─ every 2–3h ── poll recently-played ──► play_events
        │
        └─ Sunday 23:00 America/New_York
                │
                ▼
        weekly run (checkpointed per stage)
          seeds → candidates → resolve → filter → score → playlist
                │
                ▼
        Spotify playlist in the user's library
```

Stack:

- Python 3.12, `httpx` for both APIs, Pydantic for config and response models
- Postgres 16, SQLAlchemy 2, Alembic
- Typer CLI (`auth`, `profile`, `poll`, `generate`, `runs`)
- APScheduler inside a long-running container, timezone `America/New_York`, so Sunday night does not drift between EST and EDT
- Docker Compose for the app and Postgres

A hosted UI, a queue, and a separate worker are unnecessary for one user. The weekly run is a few minutes of API calls. Idempotency lives in the database, not in a job framework.

OAuth scopes:

`user-read-private`, `user-read-recently-played`, `user-top-read`, `user-library-read`, `playlist-modify-private`

Private playlists keep the experiment out of the user’s public profile. `user-read-private` is what makes `GET /me` return the user id used to create the playlist.

## Data model

Store play events and canonical tracks separately. The novelty check is “have we ever seen this recording,” and the same song on a deluxe edition has a different Spotify id.

**`users`** — one row in v1. Spotify user id, country (search `market`), encrypted refresh token, access token, expiry. Re-save the refresh token whenever Spotify rotates it.

**`tracks`** — one row per Spotify track id. Name, primary artist, artist ids, album, duration, release date, popularity, `normalized_key`. The key is `normalize(primary artist) + normalize(title)` after stripping `feat.`, `ft.`, punctuation, and remaster tags. Filtering uses both Spotify id and this key, so a remaster of a song the user has played still gets excluded.

**`play_events`** — unique on `(user_id, spotify_track_id, played_at)`. Context URI when Spotify sends one, so a later query can tell that a play came from a generated playlist. Null track ids (local files) are dropped.

**`recommendation_runs`** — one row per attempt. Unique on `(user_id, week_start)` so a retry cannot create a second playlist for the same week. Status, Spotify playlist id, and a stats blob: seeds, candidates per source, resolve misses, hard excludes, soft excludes, selected count, gap warnings.

**`recommendation_items`** — every track placed on a generated playlist. Run id, track id, source (`similar_artist`, `similar_track`, `deep_cut`), raw and normalized scores, component breakdown. This table is the soft-exclude list.

**`resolutions`** — cache from `(normalized artist, normalized title)` to a Spotify track id, or to a recorded miss. This is what keeps week two from re-searching week one’s catalog.

**`lastfm_cache`** — method, params hash, JSON body, fetched_at. TTL on the order of 30 days.

**`job_runs`** — poll and generate attempts with start, finish, status, and error. This is how the “unattended for consecutive weeks” criterion is proven.

Tokens are encrypted with a Fernet key from the environment. The refresh token is the only long-lived secret in the database.

## End to end

### 1. One-time connect

`music-discovery auth` binds `127.0.0.1` to the redirect URI registered on the Spotify app, opens the authorize URL, and exchanges the code at `https://accounts.spotify.com/api/token`. It then calls `GET /me` and upserts the user. The Spotify account must be on the app’s development-mode allowlist before this, or the consent screen fails.

There is no session and no login screen after that. The poll job refreshes the access token when it is inside a minute of expiry, and again on a 401.

### 2. History poll

Request `limit=50`. Persist each item idempotently. Advance a cursor to the newest `played_at`.

If the response contains 50 items and the oldest `played_at` is newer than the previous cursor, the window overflowed. Write a gap warning on `job_runs`. Those missed plays can still be recommended, which is the main way the hard rule leaks. A two-to-three-hour schedule makes that rare for normal listening; the warning tells you when it was not.

Also treat saved tracks as known music. Pull `GET /me/tracks` on a daily cadence and mark those track ids and normalized keys excluded, even though they may never appear in `recently-played`. Saved tracks are seeds, and they are ineligible as results. Otherwise the first playlists fill with songs already in the library that simply have not been played since connecting.

### 3. Weekly pipeline

The run loads the user, refreshes the token, and inserts a `recommendation_runs` row for the current week. If that week already has a playlist id, the job exits.

**Seeds.** Pull top artists and top tracks for `medium_term` and `long_term` (about six months, and several years). Use `short_term` lightly so the playlist does not collapse into whatever was binged this month. Sample saved tracks as extra seeds rather than walking the entire library every time. Cap seeds at about 30 artists and 40 tracks. More seeds multiply Last.fm calls without improving the top 25.

**Candidates, three sources, in parallel where rate limits allow.**

*Similar artists, then their top tracks.* For each seed artist, `artist.getSimilar` with a small limit (about 20) and `autocorrect=1`. Keep the artist-level `match`. For the best unique similar artists, `artist.getTopTracks` and take a handful of tracks. A candidate’s similarity is the parent artist match times a rank decay, so track five of a loosely related artist does not tie track one of a near neighbor.

*Similar tracks.* For each seed track, `track.getSimilar`. Store the raw match. Do not compare it with artist matches until both are normalized inside the run.

*Deep cuts.* For artists the user already likes, list albums (`include_groups=album`, skip compilations), then album tracks. Batch-fetch full track objects so popularity is available. Drop the artist’s obvious hits (high popularity, or anything Last.fm ranks in their top tracks) and anything already in history. What remains is catalog the user likes the artist for but has not played. These candidates have no Last.fm match; they carry an inverse-popularity signal and a genre overlap of 1.0 because the artist is already the user’s.

Throttle Last.fm to about 4 requests per second. On error 29 or HTTP 429, exponential backoff. Read the cache before every call.

**Resolve names to Spotify URIs.** Last.fm returns strings. Search with `track:"…" artist:"…"` and `market` set from the user profile. Score the top few hits: primary-artist equality after normalization, title similarity, and a penalty for compilations. Accept only above a high threshold. Write hits and misses to `resolutions`. Expect a real miss rate, especially on obscure similar-track results, and drop those candidates.

**Dedupe and filter.**

1. Collapse candidates that share a Spotify id or a normalized key. Keep the highest similarity and record every source that produced it.
2. Hard-exclude if the id or the normalized key is in `play_events` or in saved tracks. Lifetime. The pool shrinks over months; that is the point. The remedy if a run goes thin is deeper catalog and adjacent artists, not expiring history.
3. Soft-exclude if the track was put on a generated playlist in the last six months and never showed up in `play_events`. A later play promotes it to a permanent hard exclude via the poll.

**Score.** After per-source normalization onto 0–1:

```
score = 0.55 * similarity
      + 0.30 * genre_overlap
      + 0.15 * recency
```

Genre overlap is the Jaccard similarity between the candidate’s artist genres and the genre set of the seed artists. Recency is a mild boost for a newer release date, not a requirement. Deep cuts use inverse popularity in place of Last.fm similarity so they can compete without pretending to have a match score.

Weights live in config. The component breakdown stored on `recommendation_items` is what you inspect when a week feels wrong.

**Select 25 with quotas, not a pure sort.** A pure top-25 collapses onto one cluster of similar artists. Fill roughly 10 from similar artists, 10 from similar tracks, and 5 from deep cuts, then backfill from the global ranking if a source is thin. Cap at 2 tracks per artist and 3 tracks descended from the same seed. If fewer than 15 tracks survive, do not publish a short playlist. Mark the run failed, keep the stats, and leave the week empty rather than shipping a list that breaks the product’s bar.

**Publish.** Create a private playlist named with the run date. Add the track URIs in one request (the add-items cap is 100). Store the playlist id on the run before considering the job done. Ordering follows score within each quota block.

**Close the loop.** Anything actually played from that playlist is another `recently-played` row on the next poll. It becomes a hard exclude. Anything skipped never crosses Spotify’s 30-second bar, stays out of history, and is soft-excluded until the six-month window expires.

## Failure behavior

| Failure | Behavior |
|---|---|
| Token revoked | Poll and generate fail the job, log it, and stop. Re-run `auth`. Do not spin. |
| Spotify or Last.fm 429 | Back off and resume the same run. Candidates and resolutions are checkpointed, so a retry does not start from zero and does not create a second playlist. |
| History gap | Continue, but record the warning. The hard rule is only as good as the poll. |
| Resolve miss | Drop that candidate. |
| Fewer than 15 survivors | Fail the run with no playlist. |
| Process dies after playlist create but before the id is saved | The week key plus a Spotify-side check for an existing `Fresh — {date}` playlist makes the retry adopt that playlist instead of creating another. |

## Module layout

```
src/music_discovery/
  config.py                 # env: client ids, database URL, encryption key, weights, caps
  db.py / models.py
  spotify/auth.py           # code flow, refresh
  spotify/client.py         # throttled methods used above
  lastfm/client.py          # get_similar_artists, get_top_tracks, get_similar_tracks
  jobs/poll_history.py
  jobs/weekly.py            # orchestrates the stages
  pipeline/seeds.py
  pipeline/candidates.py
  pipeline/resolve.py
  pipeline/filter.py
  pipeline/score.py
  pipeline/publish.py
  cli.py
```

Filter and score are pure functions over in-memory candidates and sets of excluded keys. Those two modules are where the tests live. HTTP is covered with recorded fixtures, not live calls. A fixture run that feeds known history and known candidates and asserts the played track is absent is the regression test for the product promise.

## Build order

1. Schema, token encryption, and the localhost OAuth command. Stop when `GET /me` works from a stored refresh token.
2. History poll, saved-track exclude, and gap detection. Let it run for several days before trusting a playlist. The first week’s history is the thinnest the system will ever have.
3. Last.fm client, cache, and Spotify search resolution, exercised from the CLI on a handful of seeds.
4. The three candidate sources, filter, score, and quota selection, printing a dry-run list and writing a run row with no playlist.
5. Playlist publish, the week idempotency key, and the scheduler.
6. A second dry-run a week later that proves last week’s tracks are soft-excluded and anything played is hard-excluded.

## Locked decisions

- **Lifetime hard exclude** for observed plays and for saved tracks, matched on Spotify id and on normalized artist + title. Revisit only if a run falls under 15 candidates for more than one week.
- **Six-month soft exclude** for recommended-but-unplayed tracks.
- **Passive delivery.** The playlist appearing is the notification.
- **Poll every two to three hours**, not once a day. Daily polling drops history on any heavy listening day and silently breaks the guarantee.
- **No swipe feed and no LLM in v1.** Previews are gone for a new app, and a model must not invent tracks. If a later version reranks, it only reorders ids the pipeline already resolved.

The portfolio surface is the part that is easy to under-build: OAuth with rotating refresh tokens, a poll that knows when Spotify’s window ate history, a growing exclude set, and a recommender that joins two catalogs and still refuses to play a song it has already seen.
