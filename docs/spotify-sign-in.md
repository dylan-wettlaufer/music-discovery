# Spotify sign-in

`music-discovery auth` connects the single Spotify account this service uses. Run it on the host, once, after Postgres is up and `.env` has a client id, client secret, redirect URI, and Fernet key.

## What it does

1. Checks that `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, and `TOKEN_ENCRYPTION_KEY` are set, and that Postgres accepts a connection.
2. Opens the Spotify authorize URL in a browser. If the browser cannot open, it prints the URL.
3. Listens on the redirect URI (default `http://127.0.0.1:8888/callback`) for up to three minutes.
4. Checks the OAuth `state` value, exchanges the authorization code, and reads `GET /me` for the Spotify user id and country.
5. Encrypts the refresh token and access token with Fernet and stores them on the `users` row.

A later sign-in updates that same row. v1 keeps one user.

The redirect must be allowlisted on the Spotify app. The callback is localhost, so this command cannot run inside the app container.

## Scopes

The authorize URL requests:

- `user-read-private`
- `user-read-recently-played`
- `user-top-read`
- `user-library-read`
- `playlist-modify-private`

## Tokens after sign-in

`ensure_access_token` decrypts the stored access token and refreshes it when it expires within a minute. Spotify may rotate the refresh token; the new one is encrypted and saved. Profile, poll, and the scheduler all use this path.

If Spotify rejects the token, sign in again with `music-discovery auth`.
