"""Create the private Spotify playlist."""

from collections.abc import Sequence
from datetime import date

from music_discovery.spotify.client import SpotifyClient

PLAYLIST_DESCRIPTION = "Tracks you have not played."


def playlist_name(day: date) -> str:
    """Stable name for the Sunday that opens the week, so a retry can find it."""
    return f"Fresh — {day.strftime('%b')} {day.day}"


def publish_playlist(
    client: SpotifyClient,
    *,
    user_id: str,
    day: date,
    track_ids: Sequence[str],
) -> str:
    """Create ``Fresh — {date}`` and add the selected URIs.

    One playlist per user per week. A retry adopts an existing playlist of that name.
    """
    name = playlist_name(day)
    playlist_id = client.find_playlist_id(name)
    if playlist_id is None:
        playlist_id = client.create_playlist(user_id, name, description=PLAYLIST_DESCRIPTION)
    uris = [f"spotify:track:{track_id}" for track_id in track_ids]
    if uris:
        client.replace_playlist_tracks(playlist_id, uris)
    return playlist_id
