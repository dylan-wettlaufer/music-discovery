"""Long-running APScheduler process for the Compose service."""

import logging
from collections.abc import Callable
from functools import wraps

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from music_discovery.config import get_settings
from music_discovery.jobs.poll_history import run_recently_played, run_saved_tracks
from music_discovery.jobs.weekly import run_weekly

logger = logging.getLogger("music_discovery.jobs.scheduler")


def _guard(fn: Callable[[], None]) -> Callable[[], None]:
    @wraps(fn)
    def wrapper() -> None:
        try:
            fn()
        except NotImplementedError as exc:
            logger.warning("%s", exc)
        except Exception:
            logger.exception("%s failed", fn.__name__)

    return wrapper


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    scheduler = BlockingScheduler(timezone=settings.timezone)
    scheduler.add_job(
        _guard(run_recently_played),
        IntervalTrigger(hours=settings.poll_interval_hours),
        id="poll-recently-played",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _guard(run_saved_tracks),
        CronTrigger(
            hour=settings.saved_tracks_hour,
            minute=0,
            timezone=settings.timezone,
        ),
        id="poll-saved-tracks",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _guard(run_weekly),
        CronTrigger(
            day_of_week=settings.weekly_day_of_week,
            hour=settings.weekly_hour,
            minute=settings.weekly_minute,
            timezone=settings.timezone,
        ),
        id="weekly-generate",
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "scheduler started timezone=%s poll_every=%sh weekly=%s %02d:%02d",
        settings.timezone,
        settings.poll_interval_hours,
        settings.weekly_day_of_week,
        settings.weekly_hour,
        settings.weekly_minute,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
