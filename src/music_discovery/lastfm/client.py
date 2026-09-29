"""Last.fm similarity.

Throttle to about 4 requests per second. Error 29 and HTTP 429 back off.
Read ``lastfm_cache`` before every call. ``artist.getSimilar`` match is 0–1.
``track.getSimilar`` match is an unbounded float. ``artist.getTopTracks``
returns playcount, not similarity.
"""


class LastFmClient:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def get_similar_artists(self, artist: str, *, limit: int = 20) -> None:
        """artist.getSimilar with autocorrect=1."""
        del artist, limit
        raise NotImplementedError("artist.getSimilar is not implemented.")

    def get_top_tracks(self, artist: str, *, limit: int = 10) -> None:
        """artist.getTopTracks. Used to drop obvious hits from deep cuts."""
        del artist, limit
        raise NotImplementedError("artist.getTopTracks is not implemented.")

    def get_similar_tracks(self, artist: str, track: str, *, limit: int = 20) -> None:
        """track.getSimilar. Keep the raw match for per-source normalization."""
        del artist, track, limit
        raise NotImplementedError("track.getSimilar is not implemented.")
