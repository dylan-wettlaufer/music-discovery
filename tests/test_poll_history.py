from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from music_discovery.jobs.poll_history import detect_gap, ingest_recently_played, ingest_saved_tracks
from music_discovery.models import PlayEvent, SavedTrack, Track, User
from music_discovery.normalize import normalized_key
from music_discovery.spotify.client import PlayedItem, RecentlyPlayed, SavedItem, SavedTracks, SpotifyTrack
from tests.history_db import history_session

_START = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def _track(track_id: str, title: str, artist: str = "Radiohead") -> SpotifyTrack:
    return SpotifyTrack(
        id=track_id,
        name=title,
        primary_artist=artist,
        artist_ids=[f"artist-{track_id}"],
        album_name="OK Computer",
        duration_ms=200000,
        release_date="1997-06-16",
        popularity=10,
    )


def _played(track: SpotifyTrack | None, played_at: datetime, context_uri: str | None = None) -> PlayedItem:
    return PlayedItem(track=track, played_at=played_at, context_uri=context_uri)


def _window(count: int, *, oldest: datetime, track: SpotifyTrack | None = None) -> RecentlyPlayed:
    chosen = track or _track("track-0", "Karma Police")
    items = [
        _played(chosen, oldest + timedelta(minutes=index), "spotify:playlist:fresh")
        for index in range(count)
    ]
    skipped = sum(1 for item in items if item.track is None)
    return RecentlyPlayed(item_count=count, items=items, skipped_local=skipped)


def _user(session) -> User:
    user = User(spotify_user_id="spotify-user", refresh_token_encrypted="encrypted")
    session.add(user)
    session.flush()
    return user


def test_detect_gap_ignores_the_first_full_window():
    played_at = [_START + timedelta(minutes=index) for index in range(50)]
    assert detect_gap(50, played_at, None) is False


def test_detect_gap_when_the_window_moved_past_the_cursor():
    cursor = _START
    played_at = [cursor + timedelta(minutes=index + 1) for index in range(50)]
    assert detect_gap(50, played_at, cursor) is True


def test_detect_gap_when_the_oldest_play_is_still_the_cursor():
    cursor = _START
    played_at = [cursor + timedelta(minutes=index) for index in range(50)]
    assert detect_gap(50, played_at, cursor) is False
    assert detect_gap(49, [_START + timedelta(hours=5)], cursor) is False


def test_first_full_window_stores_plays_without_a_gap():
    page = _window(50, oldest=_START)
    with history_session() as session:
        user = _user(session)
        stats = ingest_recently_played(session, user, page)
        session.flush()
        stored = session.scalar(select(func.count()).select_from(PlayEvent))

    assert stats.gap_warning is False
    assert stats.inserted == 50
    assert stats.fetched == 50
    assert stats.cursor == _START + timedelta(minutes=49)
    assert stored == 50


def test_overflowed_window_flags_a_gap_and_advances_the_cursor():
    page = _window(50, oldest=_START + timedelta(hours=3))
    with history_session() as session:
        user = _user(session)
        user.recently_played_cursor = _START
        stats = ingest_recently_played(session, user, page)

    assert stats.gap_warning is True
    assert stats.cursor == _START + timedelta(hours=3, minutes=49)


def test_window_that_still_contains_the_cursor_is_not_a_gap():
    page = _window(50, oldest=_START)
    with history_session() as session:
        user = _user(session)
        user.recently_played_cursor = _START
        stats = ingest_recently_played(session, user, page)

    assert stats.gap_warning is False
    assert stats.cursor == _START + timedelta(minutes=49)


def test_a_second_poll_does_not_duplicate_plays_or_saved_tracks():
    played = RecentlyPlayed(
        item_count=1,
        items=[_played(_track("track-1", "Karma Police"), _START, "spotify:playlist:fresh")],
        skipped_local=0,
    )
    saved = SavedTracks(
        items=[SavedItem(track=_track("saved-1", "Yellow", "Coldplay"), added_at=_START)],
        skipped_local=0,
        fetched=1,
    )
    with history_session() as session:
        user = _user(session)
        first_play = ingest_recently_played(session, user, played)
        second_play = ingest_recently_played(session, user, played)
        first_saved = ingest_saved_tracks(session, user, saved)
        second_saved = ingest_saved_tracks(session, user, saved)
        session.flush()
        play_count = session.scalar(select(func.count()).select_from(PlayEvent))
        saved_count = session.scalar(select(func.count()).select_from(SavedTrack))
        context = session.scalar(select(PlayEvent.context_uri))

    assert first_play.inserted == 1
    assert second_play.inserted == 0
    assert first_saved.inserted == 1
    assert second_saved.inserted == 0
    assert play_count == 1
    assert saved_count == 1
    assert context == "spotify:playlist:fresh"


def test_unsaved_track_stays_in_the_library_table():
    kept = SavedItem(track=_track("saved-1", "Yellow", "Coldplay"), added_at=_START)
    gone = SavedTracks(items=[], skipped_local=0, fetched=0)
    with history_session() as session:
        user = _user(session)
        ingest_saved_tracks(session, user, SavedTracks(items=[kept], skipped_local=0, fetched=1))
        ingest_saved_tracks(session, user, gone)
        session.flush()
        remaining = session.scalar(select(func.count()).select_from(SavedTrack))

    assert remaining == 1


def test_remaster_gets_its_own_id_and_the_same_normalized_key():
    original = _track("original", "Karma Police")
    remaster = _track("remaster", "Karma Police (Remastered)")
    page = RecentlyPlayed(
        item_count=2,
        items=[
            _played(original, _START),
            _played(remaster, _START + timedelta(minutes=1)),
        ],
        skipped_local=0,
    )
    with history_session() as session:
        user = _user(session)
        ingest_recently_played(session, user, page)
        session.flush()
        rows = session.scalars(select(Track).order_by(Track.spotify_track_id)).all()
        ids = [row.spotify_track_id for row in rows]
        keys = [row.normalized_key for row in rows]

    assert ids == ["original", "remaster"]
    assert keys[0] == keys[1]
    assert keys[0] == normalized_key("Radiohead", "Karma Police")


def test_a_later_sighting_updates_track_fields():
    first = _track("track-1", "Karma Police")
    first.popularity = 10
    later = _track("track-1", "Karma Police")
    later.popularity = 80
    with history_session() as session:
        user = _user(session)
        ingest_recently_played(session, user, _window(1, oldest=_START, track=first))
        ingest_recently_played(
            session,
            user,
            _window(1, oldest=_START + timedelta(minutes=5), track=later),
        )
        session.flush()
        stored = session.scalar(select(Track))
        popularity = stored.popularity if stored is not None else None
        plays = session.scalar(select(func.count()).select_from(PlayEvent))

    assert popularity == 80
    assert plays == 2


def test_local_files_and_nameless_tracks_are_skipped():
    local = _played(None, _START)
    nameless = _played(
        SpotifyTrack(id="blank", name="   ", primary_artist="Radiohead", artist_ids=[]),
        _START + timedelta(minutes=1),
    )
    page = RecentlyPlayed(item_count=2, items=[local, nameless], skipped_local=1)
    with history_session() as session:
        user = _user(session)
        stats = ingest_recently_played(session, user, page)
        session.flush()
        stored = session.scalar(select(func.count()).select_from(PlayEvent))

    assert stats.inserted == 0
    assert stats.skipped == 2
    assert stats.cursor == _START + timedelta(minutes=1)
    assert stored == 0
