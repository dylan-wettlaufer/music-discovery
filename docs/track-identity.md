# Track identity

Two recordings of the same song can have different Spotify ids (a remaster, a deluxe edition, a different market). Exclusion uses both the Spotify track id and a normalized artist + title key.

`normalize` in `src/music_discovery/normalize.py`:

1. Decomposes Unicode and drops combining marks, so `é` matches `e`.
2. Removes remaster tags, including a leading year and surrounding brackets (`2011 Remaster`, `- Remastered`).
3. Removes featuring credits (`feat.`, `ft.`, `featuring`).
4. Turns other punctuation into spaces, collapses whitespace, and casefolds.

`normalized_key(artist, title)` joins the two normalized strings with a unit separator. Ingest writes that key on every `tracks` row. The candidate filter compares the same function, so a heard song and a later remaster land on one key.

The primary artist is the first artist on the Spotify track. Featuring text that lives in the title is stripped; other credited artists are stored in `artist_ids` and are not part of the key.
