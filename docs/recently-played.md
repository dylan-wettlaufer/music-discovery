# Stored plays

`music-discovery played` lists play events already stored by [listening-history ingest](listening-history.md). It reads Postgres and does not call Spotify.

Rows are newest first. Each row shows the index, primary artist, track name, and the play time in the local timezone (`Tue, Oct 6, 2026  9:04 PM`). Plays from the last five weeks also show a relative age (`3h ago`, `2d ago`, `4w ago`). A section break separates calendar days.

The caption is the play count. An empty table prints `No plays recorded yet.` If Postgres is down, the command exits with the database error.
