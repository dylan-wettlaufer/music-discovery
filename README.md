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

Put the Fernet key in `TOKEN_ENCRYPTION_KEY`, and set `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`. The redirect URI in `.env` must be allowlisted on the Spotify app. Last.fm can wait until that client is implemented.

```bash
docker compose up
```

That starts Postgres and the scheduler. The scheduler migrates the database and registers three jobs: recently-played every 3 hours, saved tracks daily at 06:00, and the weekly run Sunday at 23:00 `America/New_York`. The weekly run is not implemented yet. When it fires, it logs that and returns.

From the host, against the published Postgres port:

```bash
alembic upgrade head
music-discovery auth      # one-time Spotify OAuth
music-discovery poll       # recently-played + saved tracks
music-discovery generate   # weekly playlist; dry-run until publish is wired
music-discovery runs       # job and recommendation history
```

`auth` opens a browser on the host and stores an encrypted refresh token. `poll` reads recently-played and saved tracks into Postgres. `generate` exits until that stage is built. `runs` reads Postgres.

Do not call Spotify's recommendations, related-artists, audio-features, or audio-analysis endpoints.

## Tests

```bash
pytest
```

Filter and score are pure and covered without live Spotify or Last.fm calls. A track in known history, by Spotify id or by normalized artist + title, is absent from the selected set.

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
