# Job history

`music-discovery runs` prints the latest 20 job runs and the latest 20 recommendation runs. It reads Postgres and does not call Spotify.

## Jobs

Columns: id, job name, status, start time, and notes.

Job names written today are `recently_played`, `saved_tracks`, and `weekly_generate`. Status `succeeded` is green and `failed` is red. Notes show `gap` when the recently-played window overflowed, and the error text when the run failed.

An empty table prints `No job runs yet.`

## Recommendations

Columns: id, week start, status, and Spotify playlist id.

`music-discovery generate` and the Sunday job write these rows. Status is `dry_run` for a preview, `published` when a playlist exists, and `failed` when fewer tracks survive than the minimum. The playlist column shows the Spotify playlist id after `--publish`.

An empty table prints `No recommendation runs yet.` If Postgres is down, the command exits with the database error.
