"""Weekly recommendation run. Not implemented."""

from datetime import date, timedelta


def week_start(day: date) -> date:
    """Sunday that opens the Sunday–Saturday week containing ``day``.

    The job runs Sunday 23:00 America/New_York. A retry on Monday must keep
    the same ``(user_id, week_start)`` key.
    """
    days_since_sunday = (day.weekday() + 1) % 7
    return day - timedelta(days=days_since_sunday)


def run_weekly() -> None:
    """Run seeds → candidates → resolve → filter → score. Publishing is separate."""
    raise NotImplementedError("Weekly playlist generation is not implemented.")
