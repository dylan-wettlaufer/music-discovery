# music-discovery

A single-user background service that creates a private Spotify playlist every week, made only of tracks that are not already in observed listening history or the saved library. Exclusion is on Spotify track id and on normalized artist + title.

Product scope is in [spotify-discovery-prd.md](spotify-discovery-prd.md). Implementation detail is in [technical-deep-dive.md](technical-deep-dive.md).

## Setup

Python 3.12, Postgres 16, Docker Compose.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put the Fernet key in `TOKEN_ENCRYPTION_KEY`, and set `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, and `LASTFM_API_KEY`. The redirect URI in `.env` must be allowlisted on the Spotify app. Generate refuses to run without the Last.fm key.

```bash
docker compose up
```

That starts Postgres and the scheduler. The scheduler migrates the database and registers three jobs: recently-played every 3 hours, saved tracks daily at 06:00, and the weekly run Sunday at 23:00 `America/New_York`. The weekly run publishes one private playlist.

From the host, against the published Postgres port:

```bash
alembic upgrade head
music-discovery auth
music-discovery profile
music-discovery poll
music-discovery played
music-discovery generate
music-discovery runs
```

Feature pages are in [docs/README.md](docs/README.md).

## Commands

Run these on the host after migrate, with `.env` loaded. `auth` binds `127.0.0.1`, so it has to run on the host.

### `music-discovery auth`

One-time Spotify sign-in. Opens the authorize URL, catches the localhost redirect, and stores an encrypted refresh token for the single user. Sign in again if Spotify revokes the token. Details: [docs/spotify-sign-in.md](docs/spotify-sign-in.md).

### `music-discovery profile`

Prints the signed-in account from `GET /me`: display name, Spotify id, country, and subscription, plus email, followers, explicit filter, profile URL, and URI when Spotify sends them. Details: [docs/profile.md](docs/profile.md).

### `music-discovery poll`

Fetches recently played tracks, then the saved library, and writes both to Postgres. Prints how many tracks were seen and how many were new. If the 50-play window no longer reaches the last cursor, the summary says some plays were missed. Each half records a job run. Details: [docs/listening-history.md](docs/listening-history.md).

### `music-discovery played`

Lists stored play events, newest first: artist, track, and local play time, with a relative age for recent plays. Reads Postgres. Details: [docs/recently-played.md](docs/recently-played.md).

### `music-discovery generate`

Builds this week's playlist of tracks that are not in listening history or the saved library. The default is a dry run: it prints the tracks and does not create a playlist. `music-discovery generate --publish` writes a private playlist named `Fresh — {Mon} {day}`. Details: [docs/weekly-playlist.md](docs/weekly-playlist.md).

### `music-discovery runs`

Prints the latest 20 job runs and the latest 20 recommendation runs. Job rows show status, start time, a gap flag, and any error. Recommendation rows show the week and playlist id. Details: [docs/job-history.md](docs/job-history.md).

Do not call Spotify's recommendations, related-artists, audio-features, or audio-analysis endpoints.

## Tests

```bash
pytest
```

Filter and score are pure and covered without live Spotify or Last.fm calls. A track in known history, by Spotify id or by normalized artist + title, is absent from the selected set. How they behave: [docs/candidate-filter.md](docs/candidate-filter.md) and [docs/scoring.md](docs/scoring.md).

## Layout

```
src/music_discovery/
  config.py
  db.py
  models.py
  spotify/          auth, client
  lastfm/client.py
  jobs/             poll, weekly run, scheduler
  pipeline/         seeds, candidates, resolve, filter, score, publish
  cli.py
```
