# music-discovery

A single-user background service that creates a private Spotify playlist every week, made only of tracks the user has never played. Exclusion is enforced in Postgres on Spotify track id and on normalized artist + title. v1 has no UI: one-time OAuth, then unattended jobs. Scope is in `spotify-discovery-prd.md`; implementation is in `technical-deep-dive.md`.

Stack: Python 3.12, Postgres 16, Docker Compose, Typer CLI.

## Run

```bash
docker compose up
music-discovery auth      # one-time Spotify OAuth
music-discovery poll       # recently-played + saved tracks
music-discovery generate   # weekly playlist (dry-run until publish is wired)
music-discovery runs       # job and recommendation history
```

Do not call Spotify's recommendations, related-artists, audio-features, or audio-analysis endpoints.

## Tests

```bash
pytest
```

Unit-test `pipeline/filter.py` and `pipeline/score.py`. Cover HTTP with recorded fixtures, never live Spotify or Last.fm calls. The product check: a track in known history (same Spotify id or normalized artist + title) must be absent from the selected set.

Run the full suite after any code change. Every test must pass before you finish. Do not end a task with failures, skips you introduced, or an unrun suite.

## Commits

Imperative sentence, capitalized, ending with a period. Say why.

```
Document the recommendation architecture and lock PRD decisions.
```

One concern per commit. Do not commit secrets or `.env` files.

## Branches

Do not commit to `main`. Do the work on a task branch, one concern per branch.

When more than one agent is working in this repo, each agent uses its own branch and its own git worktree. Do not share a working directory with another active agent.
