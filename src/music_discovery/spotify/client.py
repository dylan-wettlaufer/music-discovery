"""Throttled Spotify Web API client.

Retry on HTTP 429. On 401, refresh the access token once and retry. Search
goes through the resolutions cache. Do not call Spotify's recommendation,
related-artist, audio-feature, or audio-analysis endpoints.
"""


class SpotifyClient:
    def __init__(self, access_token: str) -> None:
        self._access_token = access_token

    def get_me(self) -> None:
        """GET /me. Identity and market."""
        raise NotImplementedError("GET /me is not implemented.")

    def get_recently_played(self) -> None:
        """GET /me/player/recently-played. At most the last 50 qualifying plays."""
        raise NotImplementedError("GET /me/player/recently-played is not implemented.")

    def get_top_artists(self) -> None:
        """GET /me/top/artists."""
        raise NotImplementedError("GET /me/top/artists is not implemented.")

    def get_top_tracks(self) -> None:
        """GET /me/top/tracks."""
        raise NotImplementedError("GET /me/top/tracks is not implemented.")

    def get_saved_tracks(self) -> None:
        """GET /me/tracks."""
        raise NotImplementedError("GET /me/tracks is not implemented.")

    def get_artist_albums(self) -> None:
        """GET /artists/{id}/albums. Albums only; skip compilations."""
        raise NotImplementedError("GET /artists/{id}/albums is not implemented.")

    def get_album_tracks(self) -> None:
        """GET /albums/{id}/tracks."""
        raise NotImplementedError("GET /albums/{id}/tracks is not implemented.")

    def get_tracks(self) -> None:
        """Batched GET /tracks."""
        raise NotImplementedError("GET /tracks is not implemented.")

    def get_artists(self) -> None:
        """Batched GET /artists. Genres still live on the artist object."""
        raise NotImplementedError("GET /artists is not implemented.")

    def search_tracks(self) -> None:
        """GET /search?type=track, with the user's market."""
        raise NotImplementedError("GET /search is not implemented.")

    def create_playlist(self) -> None:
        """POST /users/{id}/playlists. Private."""
        raise NotImplementedError("POST /users/{id}/playlists is not implemented.")

    def add_playlist_tracks(self) -> None:
        """POST /playlists/{id}/tracks."""
        raise NotImplementedError("POST /playlists/{id}/tracks is not implemented.")
