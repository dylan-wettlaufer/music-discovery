import logging

from music_discovery.jobs.scheduler import _guard


def test_unimplemented_jobs_are_logged_without_stopping(caplog):
    def missing() -> None:
        raise NotImplementedError("Weekly playlist generation is not implemented.")

    with caplog.at_level(logging.WARNING):
        _guard(missing)()

    assert "not implemented" in caplog.text.lower()


def test_failed_jobs_are_logged_without_stopping(caplog):
    def fail() -> None:
        raise RuntimeError("token revoked")

    with caplog.at_level(logging.ERROR):
        _guard(fail)()

    assert "token revoked" in caplog.text
