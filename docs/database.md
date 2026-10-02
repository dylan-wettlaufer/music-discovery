# Database

Postgres 16 holds the user, the catalog, listening history, and the tables the weekly run will fill. SQLAlchemy models are in `src/music_discovery/models.py`. The initial migration is `alembic/versions/22dccbb6b0a5_initial_schema.py`.

From the host, after `docker compose up`:

```bash
alembic upgrade head
```

The app container runs the same upgrade on startup.

## Tables in use

| Table | Written by | Role |
|---|---|---|
| `users` | `music-discovery auth` | One Spotify account, encrypted tokens, recently-played cursor |
| `tracks` | poll | One row per Spotify track id, plus `normalized_key` |
| `play_events` | poll | A play at a timestamp. Unique on user, track, and `played_at` |
| `saved_tracks` | poll | Library membership. Unique on user and track. Rows stay after an unsave |
| `job_runs` | poll and the scheduler | Status, error, gap flag, and ingest counts |

`music-discovery played` reads `play_events` joined to `tracks`. `music-discovery runs` reads `job_runs` and `recommendation_runs`.

## Tables written by the weekly run

| Table | Role |
|---|---|
| `recommendation_runs` | One row per user per `week_start`. A row that already has a playlist id blocks a second playlist for that week |
| `recommendation_items` | Tracks placed on a published playlist. The six-month soft-exclude list |
| `resolutions` | Cache from normalized artist and title to a Spotify id, or a recorded miss |
| `lastfm_cache` | Cached Last.fm responses, keyed by method and parameter hash |

A dry run writes the recommendation run with status `dry_run` and does not insert recommendation items. Publish writes the items and the playlist id.

Tokens at rest use Fernet (`TOKEN_ENCRYPTION_KEY`). The key never goes in the database. A wrong key fails decryption instead of returning the token.
