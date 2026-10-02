# Candidate filter

`filter_candidates` in `src/music_discovery/pipeline/filter.py` is a pure function. The weekly job loads plays, saved tracks, and the six-month recommendation window from Postgres and passes them in.

## Dedupe

The same Spotify id, or the same normalized artist and title, is one song. Linked duplicates collapse together, including the case where id A matches id B by title and id B matches id C by id. The survivor keeps the highest similarity and the union of every source that produced the group.

## Excludes

Hard excludes are lifetime: play history and saved tracks, as Spotify ids and normalized keys. Soft excludes are the caller's already-windowed set of recent recommendations (the product window is six months). A hard match is counted first. A track that misses the hard set and hits the soft set is counted separately.

The result is the kept candidates plus `hard_excluded` and `soft_excluded` counts. A track that is in known history, by Spotify id or by normalized artist + title, is absent from the kept set.
