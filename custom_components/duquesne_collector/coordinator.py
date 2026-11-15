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
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_TOKEN, CONF_URL, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
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


def usage_points(body: dict[str, Any], backfill: bool) -> list[Point]:
    """
    Hourly points from a /usage response. On a backfill, days before the first hour are
    added from the daily totals as one point at local midnight, as core Opower does.
    """
    points: list[Point] = []
    for row in body.get("hourly") or []:
        start = _parse_dt(row.get("start"))
        if start is None or start.minute or start.second:
            continue
        points.append(Point(start, float(row.get("kwh") or 0), row.get("cost")))
    points.sort(key=lambda p: p.start)
    if backfill:
        first_hour_day = dt_util.as_local(points[0].start).date() if points else date.max
        tz = dt_util.get_default_time_zone()
        daily = []
        for row in body.get("daily") or []:
            try:
                day = date.fromisoformat(row.get("date") or "")
            except ValueError:
                continue
            if day < first_hour_day:
                midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
                daily.append(Point(dt_util.as_utc(midnight), float(row.get("kwh") or 0), row.get("cost")))
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
            imported = await self._import_usage()
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
        started = _parse_dt(data.status.get("started_at"))
        reference = success or started
        stale = reference is not None and now - reference >= STALE_AFTER
        self._issue(
            ISSUE_STALE,
            stale,
            last_success=dt_util.as_local(success).strftime("%Y-%m-%d %H:%M") if success else "never",
            error=(str(error.get("message")) if error_is_current else "") or "no error recorded",
        )

    async def _import_usage(self) -> int:
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
        backfill = last_start is None
        import_cost = True
        if backfill:
            fetch_from = None
        elif last_cost_start is not None:
            fetch_from = min(last_start, last_cost_start) + timedelta(hours=1)
        elif self._last_full_fetch is None or now - self._last_full_fetch >= FULL_FETCH_EVERY:
            fetch_from = None  # no cost rows yet: look for priced hours across the archive
        else:
            # Cost statistic still empty (Opower hasn't priced anything): fetch only new
            # hours, and leave cost to the daily full fetch so it can backfill from the start.
            fetch_from = last_start + timedelta(hours=1)
            import_cost = False
        body = await self.client.usage(fetch_from)
        if fetch_from is None:
            # Only once it succeeded: a timed-out full fetch must not hold off the cost
            # backfill for another day.
            self._last_full_fetch = now

        # Any full fetch includes the daily-only older days: consumption already has them
        # (it only takes rows after its last one), but a late cost backfill needs them.
        points = usage_points(body, backfill=fetch_from is None)
        consumption, cost = build_statistics(
            points, last_start, last_sum, last_cost_start, last_cost_sum, now
        )
        if not import_cost:
            cost = []
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
