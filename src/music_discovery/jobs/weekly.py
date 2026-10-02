"""Weekly recommendation run."""

import json
import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from music_discovery.config import SOFT_EXCLUDE_MONTHS, Settings, get_settings
from music_discovery.db import session_scope
from music_discovery.jobs.poll_history import JOB_RECENTLY_PLAYED, _catalog_fields, _upsert_track
from music_discovery.lastfm.client import LastFmClient, LastFmError
from music_discovery.models import (
    JobRun,
    PlayEvent,
    RecommendationItem,
    RecommendationRun,
    SavedTrack,
    Track,
    User,
)
from music_discovery.pipeline.candidates import (
    deep_cut_candidates,
    similar_artist_candidates,
    similar_track_candidates,
)
from music_discovery.pipeline.filter import filter_candidates
from music_discovery.pipeline.publish import playlist_name, publish_playlist
from music_discovery.pipeline.resolve import resolve_candidates
from music_discovery.pipeline.score import inverse_popularity, score_candidates, select_tracks
from music_discovery.pipeline.seeds import SavedSeed, gather_seeds
from music_discovery.pipeline.types import Candidate, Exclusions, ScoredCandidate, Source
from music_discovery.spotify.auth import AuthError, SessionFactory, ensure_access_token
from music_discovery.spotify.client import (
    SpotifyAuthError,
    SpotifyClient,
    SpotifyClientError,
    SpotifyTrack,
)

logger = logging.getLogger("music_discovery.jobs.weekly")

JOB_WEEKLY = "weekly_generate"


class WeeklyError(Exception):
    """The weekly playlist could not be built."""


@dataclass(frozen=True)
class WeeklyResult:
    status: str
    week_start: date
    selected: tuple[ScoredCandidate, ...]
    playlist_id: str | None
    message: str


def week_start(day: date) -> date:
    """Sunday that opens the Sunday–Saturday week containing ``day``.

    The job runs Sunday 23:00 America/New_York. A retry on Monday must keep
    the same ``(user_id, week_start)`` key.
    """
    days_since_sunday = (day.weekday() + 1) % 7
    return day - timedelta(days=days_since_sunday)


def run_weekly(
    *,
    publish: bool = True,
    settings: Settings | None = None,
    session_factory: SessionFactory | None = None,
    http_client: httpx.Client | None = None,
    sleep: Callable[[float], None] | None = None,
    today: date | None = None,
) -> WeeklyResult:
    """Run seeds → candidates → resolve → filter → score, then maybe publish.

    The scheduler publishes. ``music-discovery generate`` passes ``publish=False``
    and only prints the list.
    """
    settings = settings or get_settings()
    started = datetime.now(timezone.utc)
    if not settings.lastfm_api_key:
        error = WeeklyError("Set LASTFM_API_KEY before generating a playlist.")
        _record_failure(JOB_WEEKLY, started, error, session_factory)
        raise error
    owns_client = http_client is None
    http = http_client or httpx.Client(timeout=30)
    try:
        client = _spotify_client(settings, session_factory, http, sleep)
        with _sessions(session_factory) as session:
            result = _generate(
                session,
                client,
                settings=settings,
                publish=publish,
                today=_calendar_day(settings, today),
                sleep=sleep,
                http_client=http,
            )
        logger.info(
            "weekly status=%s selected=%s playlist=%s",
            result.status,
            len(result.selected),
            result.playlist_id,
        )
        return result
    except WeeklyError as exc:
        _record_failure(JOB_WEEKLY, started, exc, session_factory)
        raise
    except Exception as exc:
        reported = _weekly_error(exc)
        _record_failure(JOB_WEEKLY, started, reported, session_factory)
        if reported is exc:
            raise
        raise reported from exc
    finally:
        if owns_client:
            http.close()


def _generate(
    session: Session,
    client: SpotifyClient,
    *,
    settings: Settings,
    publish: bool,
    today: date,
    sleep: Callable[[float], None] | None,
    http_client: httpx.Client,
) -> WeeklyResult:
    user = session.scalar(select(User).order_by(User.id).limit(1))
    if user is None:
        raise WeeklyError("Sign in with `music-discovery auth` before generating a playlist.")
    start = week_start(today)
    run = session.scalar(
        select(RecommendationRun).where(
            RecommendationRun.user_id == user.id,
            RecommendationRun.week_start == start,
        )
    )
    if run is not None and run.spotify_playlist_id:
        session.add(
            _job_row(
                started=datetime.now(timezone.utc),
                status="succeeded",
                details={"skipped": True, "playlist_id": run.spotify_playlist_id},
            )
        )
        return WeeklyResult(
            status="published",
            week_start=start,
            selected=(),
            playlist_id=run.spotify_playlist_id,
            message=f"This week already has a playlist ({playlist_name(start)}).",
        )
    if run is None:
        run = RecommendationRun(
            user_id=user.id,
            week_start=start,
            status="running",
        )
        session.add(run)
        session.flush()
    else:
        run.status = "running"
        run.error = None

    lastfm = LastFmClient(
        settings.lastfm_api_key,
        http_client=http_client,
        sleep=sleep,
        requests_per_second=settings.lastfm_requests_per_second,
        session=session,
        cache_ttl=timedelta(days=settings.lastfm_cache_ttl_days),
    )
    saved = _saved_seeds(session, user.id)
    seeds = gather_seeds(client, saved)
    named = similar_artist_candidates(lastfm, seeds) + similar_track_candidates(lastfm, seeds)
    deep_cuts = deep_cut_candidates(client, lastfm, seeds, today=today)
    resolved, misses = resolve_candidates(
        session,
        client,
        named,
        market=user.country,
        seed_genres={genre for artist in seeds.artists for genre in artist.genres},
        today=today,
    )
    session.commit()
    pooled = [*resolved, *deep_cuts]
    exclusions = _exclusions(session, user.id, today)
    filtered = filter_candidates(pooled, exclusions)
    selection = select_tracks(
        score_candidates(filtered.candidates, settings.score_weights()),
        settings.selection_caps(),
    )
    gap_warning = _latest_gap(session)
    stats = {
        "seeds": {"artists": len(seeds.artists), "tracks": len(seeds.tracks)},
        "candidates": _source_counts(pooled),
        "resolve_misses": misses,
        "hard_excludes": filtered.hard_excluded,
        "soft_excludes": filtered.soft_excluded,
        "selected": len(selection.tracks),
        "gap_warning": gap_warning,
    }
    run.stats = stats
    run.finished_at = datetime.now(timezone.utc)
    if not selection.publishable:
        run.status = "failed"
        run.error = (
            f"Fewer than {settings.min_playlist_size} tracks survived. Nothing was published."
        )
        session.add(
            _job_row(
                started=datetime.now(timezone.utc),
                status="failed",
                error=run.error,
                details=stats,
                gap_warning=gap_warning,
            )
        )
        return WeeklyResult(
            status="failed",
            week_start=start,
            selected=(),
            playlist_id=None,
            message=run.error,
        )

    playlist_id = run.spotify_playlist_id
    if publish:
        playlist_id = publish_playlist(
            client,
            user_id=user.spotify_user_id,
            day=start,
            track_ids=[item.candidate.spotify_track_id for item in selection.tracks],
        )
        _store_items(session, client, run, selection.tracks)
        run.spotify_playlist_id = playlist_id
        run.status = "published"
        message = f"Published {playlist_name(start)} ({len(selection.tracks)} tracks)."
    else:
        run.status = "dry_run"
        message = (
            f"Dry run for {playlist_name(start)} "
            f"({len(selection.tracks)} tracks). Nothing was added to Spotify."
        )
    session.add(
        _job_row(
            started=datetime.now(timezone.utc),
            status="succeeded",
            details=stats,
            gap_warning=gap_warning,
        )
    )
    return WeeklyResult(
        status=run.status,
        week_start=start,
        selected=selection.tracks,
        playlist_id=playlist_id,
        message=message,
    )


def _store_items(
    session: Session,
    client: SpotifyClient,
    run: RecommendationRun,
    selected: Sequence[ScoredCandidate],
) -> None:
    existing = session.scalar(
        select(RecommendationItem.id).where(RecommendationItem.run_id == run.id).limit(1)
    )
    if existing is not None:
        return
    tracks = {
        track.id: track
        for track in client.get_tracks([item.candidate.spotify_track_id for item in selected])
        if track.id
    }
    for item in selected:
        candidate = item.candidate
        _ensure_track(session, tracks.get(candidate.spotify_track_id), candidate)
        session.add(
            RecommendationItem(
                run_id=run.id,
                spotify_track_id=candidate.spotify_track_id,
                source=candidate.source.value,
                raw_score=_raw_score(candidate),
                normalized_score=item.score,
                components={
                    "similarity": item.components.similarity,
                    "genre_overlap": item.components.genre_overlap,
                    "recency": item.components.recency,
                },
            )
        )
    session.flush()


def _ensure_track(session: Session, track: SpotifyTrack | None, candidate: Candidate) -> None:
    if track is not None:
        fields, kind = _catalog_fields(track)
        if kind == "ok" and fields is not None:
            _upsert_track(session, fields)
            return
    _upsert_track(
        session,
        {
            "spotify_track_id": candidate.spotify_track_id,
            "name": candidate.title,
            "primary_artist": candidate.artist,
            "artist_ids": [],
            "album_name": None,
            "duration_ms": None,
            "release_date": None,
            "popularity": candidate.popularity,
            "normalized_key": candidate.normalized_key,
        },
    )


def _raw_score(candidate: Candidate) -> float:
    if candidate.source is Source.DEEP_CUT:
        return inverse_popularity(candidate.popularity)
    return candidate.similarity


def _source_counts(candidates: Sequence[Candidate]) -> dict[str, int]:
    counts = {source.value: 0 for source in Source}
    for candidate in candidates:
        counts[candidate.source.value] = counts.get(candidate.source.value, 0) + 1
    return counts


def _saved_seeds(session: Session, user_id: int) -> list[SavedSeed]:
    rows = session.execute(
        select(Track.spotify_track_id, Track.primary_artist, Track.name, Track.artist_ids)
        .join(SavedTrack, SavedTrack.spotify_track_id == Track.spotify_track_id)
        .where(SavedTrack.user_id == user_id)
        .order_by(Track.spotify_track_id)
    ).all()
    seeds: list[SavedSeed] = []
    for track_id, artist, title, artist_ids in rows:
        artist_id = _first_artist_id(artist_ids)
        seeds.append(
            SavedSeed(
                spotify_id=track_id,
                artist=artist,
                title=title,
                artist_id=artist_id,
            )
        )
    return seeds


def _exclusions(session: Session, user_id: int, today: date) -> Exclusions:
    played_ids = set(
        session.scalars(select(PlayEvent.spotify_track_id).where(PlayEvent.user_id == user_id))
    )
    saved_ids = set(
        session.scalars(select(SavedTrack.spotify_track_id).where(SavedTrack.user_id == user_id))
    )
    hard_ids = played_ids | saved_ids
    hard_keys = set(
        session.scalars(
            select(Track.normalized_key).where(Track.spotify_track_id.in_(list(hard_ids)))
        )
    ) if hard_ids else set()
    cutoff = _months_ago(today, SOFT_EXCLUDE_MONTHS)
    soft_rows = session.execute(
        select(RecommendationItem.spotify_track_id, Track.normalized_key)
        .join(Track, Track.spotify_track_id == RecommendationItem.spotify_track_id)
        .join(RecommendationRun, RecommendationRun.id == RecommendationItem.run_id)
        .where(
            RecommendationRun.user_id == user_id,
            RecommendationRun.week_start >= cutoff,
        )
    ).all()
    soft_ids: set[str] = set()
    soft_keys: set[str] = set()
    for track_id, key in soft_rows:
        if track_id in played_ids or key in hard_keys:
            continue
        soft_ids.add(track_id)
        soft_keys.add(key)
    return Exclusions(
        track_ids=frozenset(hard_ids),
        normalized_keys=frozenset(hard_keys),
        soft_track_ids=frozenset(soft_ids),
        soft_normalized_keys=frozenset(soft_keys),
    )


def _first_artist_id(artist_ids: object) -> str | None:
    if isinstance(artist_ids, str):
        loaded = json.loads(artist_ids)
        artist_ids = loaded
    if isinstance(artist_ids, list) and artist_ids:
        return str(artist_ids[0])
    return None


def _latest_gap(session: Session) -> bool:
    gap = session.scalar(
        select(JobRun.gap_warning)
        .where(JobRun.job_name == JOB_RECENTLY_PLAYED)
        .order_by(JobRun.started_at.desc())
        .limit(1)
    )
    return bool(gap)


def _months_ago(day: date, months: int) -> date:
    month = day.month - months
    year = day.year
    while month <= 0:
        month += 12
        year -= 1
    last_day = _month_length(year, month)
    return date(year, month, min(day.day, last_day))


def _month_length(year: int, month: int) -> int:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return (next_month - timedelta(days=1)).day


def _calendar_day(settings: Settings, today: date | None) -> date:
    if today is not None:
        return today
    return datetime.now(ZoneInfo(settings.timezone)).date()


def _spotify_client(
    settings: Settings,
    session_factory: SessionFactory | None,
    http: httpx.Client,
    sleep: Callable[[float], None] | None,
) -> SpotifyClient:
    holder = {
        "token": ensure_access_token(
            settings=settings,
            http_client=http,
            session_factory=session_factory,
        )
    }

    def token() -> str:
        return holder["token"]

    def refresh() -> str:
        holder["token"] = ensure_access_token(
            settings=settings,
            http_client=http,
            session_factory=session_factory,
            force=True,
        )
        return holder["token"]

    return SpotifyClient(token, http_client=http, refresh=refresh, sleep=sleep)


def _weekly_error(exc: Exception) -> Exception:
    if isinstance(exc, (SpotifyAuthError, AuthError)):
        return WeeklyError(str(exc))
    if isinstance(exc, OperationalError):
        detail = exc.orig if exc.orig is not None else exc
        return WeeklyError(f"Database unavailable: {detail}")
    if isinstance(exc, (WeeklyError, SpotifyClientError, LastFmError)):
        return WeeklyError(str(exc))
    return WeeklyError(str(exc))


def _record_failure(
    job_name: str,
    started: datetime,
    exc: Exception,
    session_factory: SessionFactory | None,
) -> None:
    try:
        with _sessions(session_factory) as session:
            session.add(
                _job_row(started=started, status="failed", error=str(exc), finished_from=started)
            )
    except Exception:
        logger.exception("Could not record %s failure", job_name)


def _job_row(
    *,
    started: datetime | None = None,
    status: str,
    error: str | None = None,
    details: dict[str, object] | None = None,
    gap_warning: bool = False,
    finished_from: datetime | None = None,
) -> JobRun:
    return JobRun(
        job_name=JOB_WEEKLY,
        started_at=finished_from or started or datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        status=status,
        error=error,
        gap_warning=gap_warning,
        details=details,
    )


@contextmanager
def _sessions(session_factory: SessionFactory | None) -> Iterator[Session]:
    factory = session_factory or session_scope
    with factory() as session:
        yield session
