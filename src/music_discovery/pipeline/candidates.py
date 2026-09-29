"""Candidate sources. Not implemented."""


def similar_artist_candidates() -> None:
    """Last.fm ``artist.getSimilar``, then ``artist.getTopTracks``.

    Similarity is the artist match (0–1) times a rank decay.
    """
    raise NotImplementedError("Similar-artist candidates are not implemented.")


def similar_track_candidates() -> None:
    """Last.fm ``track.getSimilar``. Store the raw match; normalize it later."""
    raise NotImplementedError("Similar-track candidates are not implemented.")


def deep_cut_candidates() -> None:
    """Album tracks from artists the user already likes, with hits removed."""
    raise NotImplementedError("Deep-cut candidates are not implemented.")
