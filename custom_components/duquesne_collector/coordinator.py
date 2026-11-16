"""
Coordinator for Duquesne Light (collector).

Every hour: read the collector's /status and /bills for the sensors, and import any new
hourly kWh and cost from /usage as external statistics (Energy dashboard). The first run
backfills everything the collector has archived: hourly where it exists, and daily
totals for the older days that only have those.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_TOKEN, CONF_URL, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .api import CollectorAuthError, CollectorClient, CollectorError
from .const import (
    CONF_STATISTIC_KEY,
    COST_GRACE,
    DOMAIN,
    FULL_FETCH_EVERY,
    ISSUE_AUTH,
    ISSUE_STALE,
    ISSUE_UNREACHABLE,
    STALE_AFTER,
    UNREACHABLE_AFTER,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# How _import_usage reads the archive (see fetch()).
FULL, COST, NEW = "full", "cost", "new"
STORAGE_VERSION = 1

type CollectorConfigEntry = ConfigEntry[CollectorCoordinator]


@dataclass
class CollectorData:
    status: dict[str, Any]
    bill: dict[str, Any] | None  # newest dated bill
    bills: int
    imported_hours: int = 0
    fetched: datetime = field(default_factory=dt_util.utcnow)

    @property
    def last_success(self) -> datetime | None:
        return _parse_dt(self.status.get("last_success"))

    @property
    def usage_through(self) -> datetime | None:
        archive = self.status.get("archive") or {}
        return _parse_dt(archive.get("hourly_latest"))


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = dt_util.parse_datetime(str(value))
    return dt_util.as_utc(parsed) if parsed else None


def statistic_ids(key: str) -> tuple[str, str]:
    return f"{DOMAIN}:{key}_energy_consumption", f"{DOMAIN}:{key}_energy_cost"


# ---------------------------------------------------------------------------
# Usage → statistics rows (pure, tested on their own)
# ---------------------------------------------------------------------------


@dataclass
class Point:
    start: datetime  # UTC, top of the hour
    kwh: float
    cost: float | None


def _hourly_rows(body: dict[str, Any]) -> list[tuple[datetime, dict[str, Any]]]:
    """
    The usable hourly rows of a /usage response, as (UTC hour start, row): the one rule
    for what counts as an hour, shared by what's imported and what's counted.
    """
    rows = []
    for row in body.get("hourly") or []:
        start = _parse_dt(row.get("start"))
        if start is not None and not start.minute and not start.second:
            rows.append((start, row))
    return rows


def archived_hours(body: dict[str, Any]) -> list[datetime]:
    """The hour starts a /usage response holds (UTC), exactly the ones usage_points imports."""
    return [start for start, _ in _hourly_rows(body)]


def usage_points(body: dict[str, Any], backfill: bool) -> list[Point]:
    """
    Hourly points from a /usage response. On a backfill, days before the first hour are
    added from the daily totals as one point at local midnight, as core Opower does, and
    so is the part of the first hourly day before its first hour (the archive pages hourly
    history back a fixed number of hours, so its oldest day usually starts mid-day): that
    day's daily total less the hours it does have.
    """
    points = [Point(start, float(row.get("kwh") or 0), row.get("cost")) for start, row in _hourly_rows(body)]
    points.sort(key=lambda p: p.start)
    if backfill:
        tz = dt_util.get_default_time_zone()
        first_hour = dt_util.as_local(points[0].start) if points else None
        first_hour_day = first_hour.date() if first_hour else date.max
        daily = []
        for row in body.get("daily") or []:
            try:
                day = date.fromisoformat(row.get("date") or "")
            except ValueError:
                continue
            midnight = dt_util.as_utc(datetime(day.year, day.month, day.day, tzinfo=tz))
            if day < first_hour_day:
                daily.append(Point(midnight, float(row.get("kwh") or 0), row.get("cost")))
            elif day == first_hour_day and first_hour is not None and (first_hour.hour, first_hour.minute) != (0, 0):
                hours = [p for p in points if dt_util.as_local(p.start).date() == day]
                kwh = float(row.get("kwh") or 0) - sum(p.kwh for p in hours)
                if kwh <= 0.001:
                    continue  # nothing left over (or the daily total lags the hours)
                cost = None
                if row.get("cost") is not None and all(p.cost is not None for p in hours):
                    cost = max(0.0, float(row["cost"]) - sum(float(p.cost) for p in hours))
                daily.append(Point(midnight, kwh, cost))
        points = sorted(daily, key=lambda p: p.start) + points
    return points


def build_statistics(
    points: list[Point],
    last_start: datetime | None,
    last_sum: float,
    last_cost_start: datetime | None,
    last_cost_sum: float,
    now: datetime,
) -> tuple[list[StatisticData], list[StatisticData]]:
    """
    Consumption and cost rows after each statistic's own last row, continuing its sum.
    Cost stops at the first hour Opower hasn't priced yet (it fills in later), unless
    that hour is older than COST_GRACE, which is skipped for good.
    """
    consumption: list[StatisticData] = []
    cost: list[StatisticData] = []
    total, cost_total = last_sum, last_cost_sum
    cost_open = True
    for p in points:
        if last_start is None or p.start > last_start:
            total += p.kwh
            consumption.append(StatisticData(start=p.start, state=p.kwh, sum=total))
        if not cost_open or (last_cost_start is not None and p.start <= last_cost_start):
            continue
        if p.cost is None:
            if now - p.start < COST_GRACE:
                cost_open = False
            continue
        cost_total += float(p.cost)
        cost.append(StatisticData(start=p.start, state=float(p.cost), sum=cost_total))
    return consumption, cost


def merge_rebuild(new: list[StatisticData], stored: list[dict[str, Any]], tz: Any) -> list[StatisticData]:
    """
    A rebuilt series (rows from the archive, sums from zero) laid over what's stored,
    without losing history the archive doesn't hold (an add-on reinstall wipes it; a new
    collector starts empty).

    Stored rows before local midnight of the archive's first day are left alone, and the
    rebuilt sums continue from the last of them. An aggregate is a midnight row with no
    01:00 row beside it (a daily total, or a first day's remainder); it stands for the
    hours from 01:00 to its series' next row that day. Individual hours beat an
    aggregate, both ways, so no hour counts twice:
    - HA's aggregate whose hours the archive has, inside the archive's span, is set to
      zero;
    - the archive's aggregate whose hours HA has (a reinstalled collector's longer daily
      history, or its first day's remainder) is dropped, and HA's hours are kept.
    Anything else the archive lacks is real data HA has and the archive doesn't (rows
    newer than the archive's last one, after a reinstall or a switch to another
    collector; an hour a new collector missed), so it keeps its value. Every row from
    the first archive row on is returned, so all sums are consistent.

    stored: the series' rows as statistics_during_period returns them (start as a
    timestamp, state, sum), oldest first.
    """
    if not new:
        return []
    last = new[-1]["start"].timestamp()
    first_local = new[0]["start"].astimezone(tz)
    # Merge from local midnight of the archive's first day, so a daily total on that day
    # is merged (and superseded) even when the archive's first hour is 01:00 or later.
    cutoff = first_local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def is_midnight(start: datetime) -> bool:
        local = start.astimezone(tz)
        return (local.hour, local.minute) == (0, 0)

    def by_day(starts: list[float]) -> dict[date, list[float]]:
        days: dict[date, list[float]] = {}
        for ts in sorted(starts):
            days.setdefault(dt_util.utc_from_timestamp(ts).astimezone(tz).date(), []).append(ts)
        return days

    new_days = by_day([r["start"].timestamp() for r in new])
    stored_days = by_day([r["start"] for r in stored])

    def aggregate_span(ts: float, days: dict[date, list[float]]) -> tuple[float, float] | None:
        """
        For a midnight row with no 01:00 row beside it, the hours it stands for: from
        01:00 up to (not including) its series' next row that day, or the next midnight.
        It's a daily total, or a first day's remainder (the day's total less its later
        hours). None for a row that's just its hour.
        """
        start = dt_util.utc_from_timestamp(ts)
        if not is_midnight(start):
            return None
        day_rows = days.get(start.astimezone(tz).date(), [])
        one_am = (start + timedelta(hours=1)).timestamp()
        if one_am in day_rows:
            return None
        later = [t for t in day_rows if t > ts]
        if later:
            return one_am, later[0]
        next_midnight = (start.astimezone(tz) + timedelta(days=1)).replace(hour=0, minute=0)
        return one_am, dt_util.as_utc(next_midnight).timestamp()

    def has_rows(days: dict[date, list[float]], day: date, span: tuple[float, float]) -> bool:
        return any(span[0] <= t < span[1] for t in days.get(day, []))

    # An archive aggregate whose hours HA has (some of) individually: keep HA's hours.
    # Otherwise it would replace HA's 00:00 hour while HA's other hours stay, counting
    # them twice (a reinstalled collector's longer daily history, or its first day's
    # remainder).
    kept = []
    for r in new:
        ts = r["start"].timestamp()
        span = aggregate_span(ts, new_days)
        if span is not None and has_rows(stored_days, r["start"].astimezone(tz).date(), span):
            continue
        kept.append(r)
    new = kept
    new_starts = {r["start"].timestamp() for r in new}
    total = 0.0
    rows = [(r["start"], float(r["state"] or 0.0)) for r in new]
    for row in stored:
        ts = row["start"]
        if ts < cutoff:
            total = float(row.get("sum") or 0.0)
            continue
        if ts in new_starts:
            continue
        start = dt_util.utc_from_timestamp(ts)
        # HA's aggregate whose hours the archive has individually, inside the archive's
        # span: superseded by them.
        span = aggregate_span(ts, stored_days) if ts <= last else None
        superseded = span is not None and has_rows(new_days, start.astimezone(tz).date(), span)
        rows.append((start, 0.0 if superseded else float(row.get("state") or 0.0)))
    rows.sort(key=lambda r: r[0])
    merged: list[StatisticData] = []
    for start, state in rows:
        total += state
        merged.append(StatisticData(start=start, state=state, sum=total))
    return merged


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class CollectorCoordinator(DataUpdateCoordinator[CollectorData]):
    """Reads the collector hourly."""

    config_entry: CollectorConfigEntry

    def __init__(self, hass: HomeAssistant, entry: CollectorConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = CollectorClient(
            async_get_clientsession(hass), entry.data[CONF_URL], entry.data.get(CONF_API_TOKEN)
        )
        self._failing_since: datetime | None = None
        self._last_full_fetch: datetime | None = None
        # Hourly records we've imported, to compare with the archive's count: a mismatch
        # means the collector added hours we skipped (its first-run backfill, gap fills,
        # anything while HA was down), and the series is rebuilt. None: not yet known.
        self._known_hours: int | None = None
        # The archive's newest hour when it was last counted: hours newer than it are new
        # to the archive even if HA already has them (a replacement collector catching up).
        self._archive_through: datetime | None = None
        # Kept across restarts, so a restart doesn't have to rebuild to learn it again.
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")

    @staticmethod
    async def async_remove_state(hass: HomeAssistant, entry_id: str) -> None:
        await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}").async_remove()

    def _issue_id(self, kind: str) -> str:
        return f"{kind}_{self.config_entry.entry_id}"

    def _issue(self, kind: str, active: bool, **placeholders: str) -> None:
        issue_id = self._issue_id(kind)
        if not active:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR if kind == ISSUE_AUTH else ir.IssueSeverity.WARNING,
            translation_key=kind,
            translation_placeholders={"url": self.client.url, **placeholders},
        )

    async def _async_update_data(self) -> CollectorData:
        try:
            status = await self.client.status()
            bills = await self.client.bills()
            imported = await self._import_usage(status)
        except CollectorAuthError as err:
            # The collector answers but rejects our token (rotated on the add-on): start
            # reauth so the user can enter the new one. It isn't unreachable.
            self._track_unreachable(False)
            raise ConfigEntryAuthFailed(f"{err}; enter the collector's current API token") from err
        except CollectorError as err:
            self._track_unreachable(True)
            raise UpdateFailed(str(err)) from err
        self._track_unreachable(False)
        data = CollectorData(
            status=status,
            bill=bills.get("current"),
            bills=len(bills.get("bills") or []),
            imported_hours=imported,
        )
        self._check_health(data)
        return data

    def _track_unreachable(self, failed: bool) -> None:
        now = dt_util.utcnow()
        if not failed:
            self._failing_since = None
            self._issue(ISSUE_UNREACHABLE, False)
            return
        self._failing_since = self._failing_since or now
        if now - self._failing_since >= UNREACHABLE_AFTER:
            self._issue(ISSUE_UNREACHABLE, True, since=dt_util.as_local(self._failing_since).strftime("%Y-%m-%d %H:%M"))

    def _check_health(self, data: CollectorData) -> None:
        """Repair issues from the collector's own status: login failing, or no success lately."""
        now = dt_util.utcnow()
        error = data.status.get("last_error") or {}
        error_at = _parse_dt(error.get("at"))
        success = data.last_success
        error_is_current = error_at is not None and (success is None or error_at > success)
        self._issue(
            ISSUE_AUTH,
            error_is_current and error.get("type") == "auth",
            message=str(error.get("message") or ""),
        )
        # Without a success, measure from the collector's first start (kept across
        # restarts); started_at, which resets on every restart, only for older collectors.
        started = _parse_dt(data.status.get("first_started_at")) or _parse_dt(data.status.get("started_at"))
        reference = success or started
        stale = reference is not None and now - reference >= STALE_AFTER
        self._issue(
            ISSUE_STALE,
            stale,
            last_success=dt_util.as_local(success).strftime("%Y-%m-%d %H:%M") if success else "never",
            error=(str(error.get("message")) if error_is_current else "") or "no error recorded",
        )

    async def _import_usage(self, status: dict[str, Any]) -> int:
        key = self.config_entry.data[CONF_STATISTIC_KEY]
        consumption_id, cost_id = statistic_ids(key)
        recorder = get_instance(self.hass)

        async def last(statistic_id: str) -> tuple[datetime | None, float]:
            stat = await recorder.async_add_executor_job(
                get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
            )
            if not stat:
                return None, 0.0
            row = stat[statistic_id][0]
            return dt_util.utc_from_timestamp(row["start"]), row.get("sum") or 0.0

        last_start, last_sum = await last(consumption_id)
        last_cost_start, last_cost_sum = await last(cost_id)
        now = dt_util.utcnow()
        archive_hours = (status.get("archive") or {}).get("hourly_records")

        async def fetch(mode: str) -> tuple[list[StatisticData], list[StatisticData], list[datetime]]:
            """
            FULL re-imports the whole archive (both series rebuilt from zero). COST reads the
            whole archive to backfill a still-empty cost series, but adds only new hours to
            consumption. NEW fetches only hours after the stored rows, and after the newest
            hour the archive had when we last counted it (so hours it archives that HA
            already has are seen and counted). Also returns the hours the response held.
            """
            consumption_from = (None, 0.0) if mode == FULL else (last_start, last_sum)
            cost_from = (last_cost_start, last_cost_sum) if mode == NEW else (None, 0.0)
            if mode != NEW:
                fetch_from = None
            else:
                after = [t for t in (last_start, last_cost_start, self._archive_through) if t is not None]
                fetch_from = min(after) + timedelta(hours=1)
            body = await self.client.usage(fetch_from)
            if fetch_from is None:
                # Only once it succeeded: a timed-out full read is retried next poll.
                self._last_full_fetch = now
            points = usage_points(body, backfill=fetch_from is None)
            consumption, cost = build_statistics(points, *consumption_from, *cost_from, now)
            if mode == NEW and last_cost_start is None:
                cost = []  # still unpriced: cost backfills from the start on the daily full read
            return consumption, cost, archived_hours(body)

        async def fetch_or_fall_back(
            mode: str,
        ) -> tuple[str, list[StatisticData], list[StatisticData], list[datetime]]:
            """A full read that fails (it grows with the archive) mustn't take the sensors down."""
            try:
                return (mode, *await fetch(mode))
            except CollectorAuthError:
                raise
            except CollectorError as err:
                if mode == NEW or last_start is None:
                    raise
                _LOGGER.warning(
                    "Reading the collector's whole archive failed (%s); importing new hours only "
                    "and trying the full read again next poll",
                    err,
                )
                return (NEW, *await fetch(NEW))

        def newly_archived(hours: list[datetime], consumption: list[StatisticData]) -> int:
            """Archive hours added since we last counted: those newer than its newest hour then."""
            if self._archive_through is None:
                return len(consumption)  # a count saved before the archive's newest hour was kept
            return sum(1 for hour in hours if hour > self._archive_through)

        cost_due = last_cost_start is None and (
            self._last_full_fetch is None or now - self._last_full_fetch >= FULL_FETCH_EVERY
        )
        if self._known_hours is None and last_start is not None:
            saved = await self._store.async_load() or {}
            # Only valid for the series it was counted against: a cleared or edited
            # statistic means it has to be learned again.
            if saved.get("through") == last_start.timestamp():
                self._known_hours = saved.get("known_hours")
                through = saved.get("archive_through")
                self._archive_through = dt_util.utc_from_timestamp(through) if through else None
        if (
            self._known_hours is not None
            and archive_hours is not None
            and archive_hours < self._known_hours
        ):
            # The archive shrank (add-on reinstalled, or another collector): the count no
            # longer describes it, and hours we missed would never show as a shortfall.
            # Treat it as unknown, so this poll rebuilds (without losing history) and resyncs.
            # Checked after the load above, so a count saved before a restart is caught on
            # the first poll too.
            self._known_hours = None
            self._archive_through = None
            await self._store.async_remove()
        if last_start is None or self._known_hours is None:
            mode = FULL  # first import, or the imported count isn't known
        else:
            mode = COST if cost_due else NEW
        mode, consumption, cost, hours = await fetch_or_fall_back(mode)
        added = newly_archived(hours, consumption) if mode != FULL else 0
        if (
            mode != FULL
            and self._known_hours is not None
            and archive_hours is not None
            and self._known_hours + added < archive_hours
        ):
            # The archive holds hours we never saw (the collector's first-run backfill
            # landed after our first import, or it filled a gap): rebuild the series.
            # Only a shortfall counts: /status is read before /usage, so hours the
            # collector archives in between make the import look larger, not smaller.
            _LOGGER.debug("Collector archive has %s hours, %s imported: rebuilding", archive_hours, self._known_hours)
            try:
                consumption, cost, hours = await fetch(FULL)
                mode = FULL
            except CollectorAuthError:
                raise
            except CollectorError as err:
                _LOGGER.warning("Rebuilding from the collector's archive failed (%s); trying again next poll", err)
        if mode == FULL:
            # The archive's own count, not the response's: a record the API skips must not
            # make every later poll look like a mismatch.
            self._known_hours = archive_hours if archive_hours is not None else len(hours)
            self._archive_through = max(hours, default=None)
            # Lay the rebuilt rows over what's stored instead of clearing: clearing would
            # also delete history the archive doesn't hold.
            stored = await recorder.async_add_executor_job(
                statistics_during_period,
                self.hass,
                datetime(2000, 1, 1, tzinfo=dt_util.UTC),
                None,
                {consumption_id, cost_id},
                "hour",
                None,
                {"state", "sum"},
            )
            tz = dt_util.get_default_time_zone()
            consumption = merge_rebuild(consumption, stored.get(consumption_id, []), tz)
            cost = merge_rebuild(cost, stored.get(cost_id, []), tz)
        elif self._known_hours is not None:
            self._known_hours += added
            if hours:
                self._archive_through = max([*hours, *([self._archive_through] if self._archive_through else [])])
        newest = consumption[-1]["start"] if consumption else last_start
        if self._known_hours is not None and newest is not None:
            await self._store.async_save(
                {
                    "known_hours": self._known_hours,
                    "through": newest.timestamp(),
                    "archive_through": self._archive_through.timestamp() if self._archive_through else None,
                }
            )
        name = self.config_entry.title
        if consumption:
            async_add_external_statistics(
                self.hass,
                StatisticMetaData(
                    mean_type=StatisticMeanType.NONE,
                    has_sum=True,
                    name=f"{name} electricity consumption",
                    source=DOMAIN,
                    statistic_id=consumption_id,
                    unit_class=EnergyConverter.UNIT_CLASS,
                    unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                ),
                consumption,
            )
        if cost:
            async_add_external_statistics(
                self.hass,
                StatisticMetaData(
                    mean_type=StatisticMeanType.NONE,
                    has_sum=True,
                    name=f"{name} electricity cost",
                    source=DOMAIN,
                    statistic_id=cost_id,
                    unit_class=None,
                    unit_of_measurement=None,  # as core Opower does for cost
                ),
                cost,
            )
        _LOGGER.debug("Imported %d consumption and %d cost rows", len(consumption), len(cost))
        return len(consumption)
