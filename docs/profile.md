# Signed-in profile

`music-discovery profile` prints the Spotify account stored at sign-in, refreshed from `GET /me`.

It needs a completed `music-discovery auth` and a reachable Spotify API. A missing user, a bad token, or a Spotify error exits with a message on stderr.

## Fields

Always shown:

- Display name
- Spotify ID
- Country
- Subscription (`premium`, `free`, and `open` are labeled Premium, Free, and Open)

Shown when Spotify returns them:

- Email
- Follower count
- Explicit filter (`On` or `Off`, plus `locked` when the filter cannot be changed)
- Profile URL
- Spotify URI

Empty values render as an em dash. The command does not write to Postgres.
