from datetime import date

from music_discovery.jobs.weekly import week_start


def test_week_opens_on_sunday_and_holds_through_saturday():
    sunday = date(2026, 9, 27)
    assert week_start(sunday) == sunday
    assert week_start(date(2026, 9, 28)) == sunday
    assert week_start(date(2026, 10, 3)) == sunday
    assert week_start(date(2026, 10, 4)) == date(2026, 10, 4)
