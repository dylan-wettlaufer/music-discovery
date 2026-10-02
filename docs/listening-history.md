# Listening-history ingest

`music-discovery poll` copies recently played tracks and the saved library into Postgres. The scheduler runs the same two jobs on their own timers. See [scheduler.md](scheduler.md).

Sign in first. Polling uses the stored token and refreshes it if Spotify returns 401. HTTP 429 waits on `Retry-After` and retries a bounded number of times.

The command runs recently played, prints a summary, then saved tracks, and prints a second summary. If the token is revoked during recently played, saved tracks are skipped for that invocation. The daily saved-track job still runs on its own schedule.

## Recently played

`GET /me/player/recently-played` returns at most the last 50 tracks played for more than about 30 seconds. Older history is not available. Each poll:

- Skips local files (no Spotify id) and rows with a blank artist or title.
- Upserts the track catalog row, including a normalized artist + title key.
- Inserts a play event. The same user, track, and `played_at` is stored once.
- Moves `users.recently_played_cursor` to the newest `played_at` in the response.

If the response is a full page of 50 and the oldest play is newer than the previous cursor, plays fell out of Spotify's window before they were stored. The job still succeeds, sets `gap_warning`, and the summary says some plays were missed.

## Saved tracks

`GET /me/tracks` follows `next` until the library is exhausted. Each catalog track is upserted. A saved row is inserted once per user and track.

Unsaving a track in Spotify does not delete the row. Saved tracks stay in the library table so they can be treated as known music.

## What a run records

Each half writes a `job_runs` row named `recently_played` or `saved_tracks`.

Success stores `fetched`, `inserted`, `skipped_local`, `skipped`, the cursor when one exists, and `gap` when the window overflowed. Failure stores the error and status `failed`.

Printed summaries:

```
Recently played: 12 seen, 3 new.
Saved tracks: 400 seen, 2 new.
```

A gap appends: `The 50-play window overflowed; some plays were missed.`
