"""The random slots a monitor is scheduled at.

Pure functions, tested with a seeded generator: the point is the window each
slot falls in, not the particular second.
"""

import random
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.services.radar_monitor import first_run_slot, next_slot

NOW = datetime(2026, 9, 24, 14, 37, 12, tzinfo=UTC)


def _slots(fn, *args, n: int = 500):  # type: ignore[no-untyped-def]
    """Many draws from one seeded generator, so the window is exercised."""
    rng = random.Random(1234)

    return [fn(*args, rng) for _ in range(n)]


def test_first_run_lands_within_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MONITOR_FIRST_RUN_DELAY_MINUTES", 10)

    for slot in _slots(first_run_slot, NOW):
        assert NOW + timedelta(seconds=30) <= slot <= NOW + timedelta(minutes=10)


def test_first_run_is_never_immediate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with the delay set to zero there is a short floor, so a
    create-then-pause has a moment to land before a dispatch tick."""
    monkeypatch.setattr(settings, "MONITOR_FIRST_RUN_DELAY_MINUTES", 0)

    for slot in _slots(first_run_slot, NOW):
        assert slot >= NOW + timedelta(seconds=30)


def test_daily_slot_is_a_random_second_of_the_next_utc_day() -> None:
    tomorrow = datetime(2026, 9, 25, tzinfo=UTC)

    slots = _slots(next_slot, NOW, "daily")

    for slot in slots:
        assert tomorrow <= slot < tomorrow + timedelta(days=1)

    # Spread across the day, not bunched: with 500 draws the earliest and
    # latest hours should both be represented.
    hours = {slot.hour for slot in slots}
    assert min(hours) <= 1
    assert max(hours) >= 22


def test_weekly_slot_is_the_same_weekday_next_week() -> None:
    next_week = datetime(2026, 10, 1, tzinfo=UTC)
    assert next_week.weekday() == NOW.weekday()

    for slot in _slots(next_slot, NOW, "weekly"):
        assert next_week <= slot < next_week + timedelta(days=1)


def test_a_run_late_in_the_day_still_gets_tomorrow_not_the_day_after() -> None:
    """Anchoring to the calendar day is what makes "daily" mean daily: a run
    at 23:59 must be followed by one tomorrow, not skip a day."""
    late = datetime(2026, 9, 24, 23, 59, 59, tzinfo=UTC)
    tomorrow = datetime(2026, 9, 25, tzinfo=UTC)

    for slot in _slots(next_slot, late, "daily"):
        assert tomorrow <= slot < tomorrow + timedelta(days=1)


def test_a_run_just_after_midnight_does_not_double_up_today() -> None:
    early = datetime(2026, 9, 24, 0, 0, 5, tzinfo=UTC)
    tomorrow = datetime(2026, 9, 25, tzinfo=UTC)

    for slot in _slots(next_slot, early, "daily"):
        assert slot >= tomorrow
