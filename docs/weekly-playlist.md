# Weekly playlist

`music-discovery generate` builds this week's list of tracks that are not in listening history or the saved library. The Sunday scheduler runs the same job and publishes.

Set `LASTFM_API_KEY` first. Sign in with `music-discovery auth` so a user row exists. Poll at least once so plays and saved tracks are in Postgres; history only contains what has been observed since connect.

## Command

```bash
music-discovery generate            # dry run
music-discovery generate --publish  # create the private playlist
```

A dry run prints a table of artist, track, source, and score, then a note that nothing was added to Spotify. `--publish` creates or reuses a private playlist named `Fresh — {Mon} {day}` (the Sunday that opens the week) and replaces its tracks with the selection.

If that week already has a playlist id stored, the command reports the existing playlist and does not build another. If fewer than 15 tracks survive filtering and caps, nothing is published and the run is marked failed.

The week key is that Sunday. A retry on Monday stays on the same week.

## Pipeline

1. **Seeds.** Up to 30 top artists and 40 top tracks. Medium-term and long-term ranges lead. Short-term adds at most 5 artists and 5 tracks. Saved tracks fill leftover track slots, spread across the library. Artists missing from the top-artist lists are filled from those tracks, and their genres are loaded.
2. **Similar artists.** Last.fm `artist.getSimilar` (match 0–1). Artists the user already seeds are dropped. The top 15 matches contribute their Last.fm top tracks. Similarity is the artist match divided by rank.
3. **Similar tracks.** Last.fm `track.getSimilar` for each seed track. The raw match is kept and scaled later.
4. **Deep cuts.** Up to 3 non-compilation albums per seed artist. Tracks with popularity 70 or above, or whose names match that artist's Last.fm top tracks, are dropped. At most 8 tracks per artist remain. Similarity is inverse popularity.
5. **Resolve.** Last.fm names are searched on Spotify in the user's market. A hit needs the same normalized primary artist and a title ratio of at least 0.8. Compilations are penalized. Hits and misses are cached on `resolutions`.
6. **Filter.** Duplicates collapse. Plays and saved tracks are lifetime excludes, by Spotify id and by normalized artist + title. Recommendation items from the last 6 months are soft excludes until the user plays them. See [candidate-filter.md](candidate-filter.md).
7. **Score and select.** See [scoring.md](scoring.md). Default size is 25. Below 15, the run fails closed.
8. **Publish.** Only with `--publish`, or from the scheduler. Selected tracks are stored as `recommendation_items`, which feeds the next soft-exclude window.

Last.fm calls are throttled to about 4 per second, back off on error 29 and HTTP 429, and read `lastfm_cache` before each request. The cache TTL is 30 days.

Each attempt records a `weekly_generate` job run and a `recommendation_runs` row. `music-discovery runs` shows both.
