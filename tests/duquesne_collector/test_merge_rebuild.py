"""
merge_rebuild over every combination of what HA holds for a day and what the archive holds.

The rebuild merge took many review rounds, each fixing one more combination. This checks
them all at once: for the middle of three days, each source holds one "shape" of data,
and the merged day must not count more than the day's real use, must not lose what the
better source has (apart from the documented trade-offs in KNOWN), and the running sum
must never go down. If you change merge_rebuild, a new failure here is a combination your
change affects: decide whether it's a fix (update KNOWN) or a bug.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.duquesne_collector.coordinator import merge_rebuild

TZ = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
DAY_KWH = 24.0  # one kWh an hour; a daily total is the whole day

# What one source can hold for a day: (local hour, kWh) rows.
SHAPES: dict[str, list[tuple[int, float]]] = {
    "none": [],
    "daily": [(0, DAY_KWH)],  # a daily total at midnight
    "remainder": [(0, 13.0)] + [(h, 1.0) for h in range(13, 24)],  # first day: total of 00-12, then hours
    "hourly": [(h, 1.0) for h in range(24)],
    "hourly_no_00": [(h, 1.0) for h in range(1, 24)],  # missed its 00:00 reading
    "hourly_no_01": [(h, 1.0) for h in range(24) if h != 1],  # missed its 01:00 reading
    "only_00": [(0, 1.0)],  # an hourly day with only its first reading so far
    "one_late_hour": [(13, 1.0)],  # an hourly day with a single later reading
}

# Combinations where the merged day deliberately isn't the better source's total.
# Individual hours beat an aggregate (a daily total), so an hourly day that's missing a
# reading loses that hour against a full daily total; and an hourly source holding one
# reading for a whole day (which the collector can't produce: its hourly data starts at
# midnight, and HA holds daily totals only before its hourly history) loses to it too.
# Never a double count. Keyed (HA's shape, archive's shape): merged kWh for the day.
KNOWN: dict[tuple[str, str], float] = {
    ("daily", "hourly_no_00"): 23.0,
    ("daily", "hourly_no_01"): 23.0,
    ("hourly_no_00", "daily"): 23.0,
    ("hourly_no_01", "daily"): 23.0,
    ("remainder", "hourly_no_00"): 23.0,  # a first day's remainder is an aggregate too
    ("remainder", "hourly_no_01"): 23.0,
    ("hourly_no_00", "remainder"): 23.0,
    ("hourly_no_01", "remainder"): 23.0,
    ("daily", "only_00"): 1.0,
    ("daily", "one_late_hour"): 1.0,
    ("one_late_hour", "daily"): 1.0,
}


def _rows(day: date, shape: str) -> list[tuple[datetime, float]]:
    return [(datetime(day.year, day.month, day.day, h, tzinfo=TZ), kwh) for h, kwh in SHAPES[shape]]


def _merge(ha_shape: str, archive_shape: str, archive_ends_here: bool) -> tuple[float, list[float]]:
    """Merged kWh on the middle day, and the merged running sums."""
    first = date(2026, 3, 1)
    days = [first, first + timedelta(days=1), first + timedelta(days=2)]
    ha: list[tuple[datetime, float]] = []
    archive: list[tuple[datetime, float]] = []
    for day in days:
        if day == days[1]:
            ha += _rows(day, ha_shape)
            archive += _rows(day, archive_shape)
        else:  # both sources have the surrounding days hourly, unless the archive ends here
            ha += _rows(day, "hourly")
            if not (day == days[2] and archive_ends_here):
                archive += _rows(day, "hourly")

    stored, total = [], 0.0
    for start, kwh in sorted(ha):
        total += kwh
        stored.append({"start": start.timestamp(), "state": kwh, "sum": total})
    new, total = [], 0.0
    for start, kwh in sorted(archive):
        total += kwh
        new.append({"start": start.astimezone(UTC), "state": kwh, "sum": total})

    merged = merge_rebuild(new, stored, TZ)
    on_day = sum(r["state"] for r in merged if r["start"].astimezone(TZ).date() == days[1])
    return on_day, [r["sum"] for r in merged]


@pytest.mark.parametrize("archive_ends_here", [False, True], ids=["inside", "archive-ends"])
@pytest.mark.parametrize("archive_shape", list(SHAPES))
@pytest.mark.parametrize("ha_shape", list(SHAPES))
def test_merge_matrix(ha_shape: str, archive_shape: str, archive_ends_here: bool) -> None:
    on_day, sums = _merge(ha_shape, archive_shape, archive_ends_here)
    assert on_day <= DAY_KWH + 1e-9, "an hour counted twice"
    assert sums == sorted(sums), "the running total goes down"
    better = max(sum(k for _, k in SHAPES[ha_shape]), sum(k for _, k in SHAPES[archive_shape]))
    expected = KNOWN.get((ha_shape, archive_shape), better)
    assert on_day == pytest.approx(expected)


def test_known_cases_are_still_reachable() -> None:
    """KNOWN mustn't list shapes that no longer exist."""
    for ha_shape, archive_shape in KNOWN:
        assert ha_shape in SHAPES and archive_shape in SHAPES
