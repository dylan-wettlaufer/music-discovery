import logging

from music_discovery.jobs.scheduler import _guard


def test_unimplemented_jobs_are_logged_and_swallowed(caplog):
    def missing() -> None:
        raise NotImplementedError("Weekly playlist generation is not implemented.")

    with caplog.at_level(logging.WARNING):
        _guard(missing)()

    assert "not implemented" in caplog.text.lower()


def test_a_failed_job_is_logged_and_the_scheduler_stays_up(caplog):
    def boom() -> None:
        raise RuntimeError("database down")

    with caplog.at_level(logging.ERROR):
        _guard(boom)()

    assert "database down" in caplog.text
