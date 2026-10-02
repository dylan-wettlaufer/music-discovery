# Implemented features

These pages describe behavior that is in the code today. Product scope is in [spotify-discovery-prd.md](../spotify-discovery-prd.md). The intended architecture is in [technical-deep-dive.md](../technical-deep-dive.md).

| Feature | Command | Page |
|---|---|---|
| Spotify sign-in | `music-discovery auth` | [spotify-sign-in.md](spotify-sign-in.md) |
| Signed-in profile | `music-discovery profile` | [profile.md](profile.md) |
| Listening-history ingest | `music-discovery poll` | [listening-history.md](listening-history.md) |
| Stored plays | `music-discovery played` | [recently-played.md](recently-played.md) |
| Weekly playlist | `music-discovery generate` | [weekly-playlist.md](weekly-playlist.md) |
| Job history | `music-discovery runs` | [job-history.md](job-history.md) |
| Background schedule | Compose `app` service | [scheduler.md](scheduler.md) |
| Track identity | — | [track-identity.md](track-identity.md) |
| Candidate filter | — | [candidate-filter.md](candidate-filter.md) |
| Scoring and selection | — | [scoring.md](scoring.md) |
| Database | `alembic upgrade head` | [database.md](database.md) |

The weekly run calls the filter and scorer after it gathers seeds, asks Last.fm for similar music, and resolves those names to Spotify tracks.
