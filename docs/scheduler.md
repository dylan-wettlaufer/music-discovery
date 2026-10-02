# Scheduler

`docker compose up` starts Postgres and a long-running app container. The container entrypoint migrates the database, then runs APScheduler in `America/New_York` (or `TIMEZONE` from settings).

Three jobs are registered. Each allows one instance and coalesces a missed fire into a single run. An unexpected exception is logged and the process stays up.

| Job id | When | What runs |
|---|---|---|
| `poll-recently-played` | Every `POLL_INTERVAL_HOURS` hours (default 3, allowed 2–3) | `run_recently_played` |
| `poll-saved-tracks` | Daily at `SAVED_TRACKS_HOUR`:00 (default 06:00) | `run_saved_tracks` |
| `weekly-generate` | Sunday 23:00 (`WEEKLY_DAY_OF_WEEK`, `WEEKLY_HOUR`, `WEEKLY_MINUTE`) | `run_weekly` (publishes) |

Recently played and saved tracks are the same ingest as `music-discovery poll`. They share the encrypted token in Postgres. The container needs `TOKEN_ENCRYPTION_KEY`, the Spotify client credentials, and `LASTFM_API_KEY` from `.env`.

The Sunday job publishes one private playlist for that week. `week_start` is the Sunday that opens the Sunday–Saturday week, so a Monday retry keeps the same week key. A failure is logged and the scheduler keeps running. See [weekly-playlist.md](weekly-playlist.md).
