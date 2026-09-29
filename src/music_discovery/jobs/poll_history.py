"""Recently-played poll and saved-track ingest. Not implemented."""


def run_recently_played() -> None:
    """Append plays idempotently and warn when the 50-track window overflowed."""
    raise NotImplementedError("Recently-played poll is not implemented.")


def run_saved_tracks() -> None:
    """Refresh the library. Saved tracks are seeds and lifetime hard excludes."""
    raise NotImplementedError("Saved-track ingest is not implemented.")


def run_poll() -> None:
    """Manual poll: recently played, then saved tracks."""
    run_recently_played()
    run_saved_tracks()
