"""
Coordinator for Energy Shopper.

One config entry shops one fuel. Twice a day it fetches the market's offers (PA Gas
Switch or PA Power Switch), reads your usage from a long-term statistic and your
current rate from sensors (or typed values), and ranks the offers with
energy_shopper_core.decision (decide_gas / decide_electric), the same functions the
standalone notifier uses. Then it keeps the "cheaper supplier" and "rate jump" repair
issues in step and fires an event when the recommendation changes.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

import requests

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util, slugify
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

from .const import (
    CONF_DISTRIBUTOR,
    CONF_FUEL,
    CONF_NO_CANCELLATION_FEE,
    CONF_PRICE_UNIT,
    CONF_PRIOR_SUPPLIERS,
    CONF_PTC_ENTITY,
    CONF_PTC_VALUE,
    CONF_RATE_ENTITY,
    CONF_RATE_JUMP_PCT,
    CONF_RATE_TYPE,
    CONF_RATE_UNIT,
    CONF_RATE_VALUE,
    CONF_RENEWABLE_ONLY,
    CONF_SERVICE_TYPE,
    CONF_SUPPLIER_ENTITY,
    CONF_SUPPLIER_NAME,
    CONF_THERMS_PER_MCF,
    CONF_THRESHOLD,
    CONF_TOU_OFF_PEAK,
    CONF_TOU_PEAK,
    CONF_TOU_SUPER_OFF_PEAK,
    CONF_USAGE_STATISTIC,
    CONF_ZIPCODE,
    DEFAULT_RATE_JUMP_PCT,
    DEFAULT_SERVICE_TYPE,
    DEFAULT_THRESHOLD,
    DOMAIN,
    ELECTRIC_SHOP_URL,
    EVENT_RECOMMENDATION_CHANGED,
    FUEL_ELECTRIC,
    FUEL_GAS,
    HOURLY_LOOKBACK_DAYS,
    ISSUE_CHEAPER,
    ISSUE_RATE_JUMP,
    SHOP_URL,
    TOU_MIN_HOURS_PER_DAY,
    UPDATE_INTERVAL,
    USAGE_LOOKBACK_DAYS,
)
from .vendor.energy_shopper_core import decision
from .vendor.energy_shopper_core.eligibility import parse_prior_suppliers, prior_suppliers
from .vendor.energy_shopper_core.gas import DEFAULT_THERMS_PER_MCF
from .vendor.energy_shopper_core.markets import pa
from .vendor.energy_shopper_core.models import Rate
from .vendor.energy_shopper_core.report import RankedOption
from .vendor.energy_shopper_core.tou import HourlyUsageRecord, TOUAnalysisResult, TOUAnalyzer, TOURates
from .vendor.energy_shopper_core.usage import (
    UsageWindow,
    project_annual_kwh_from_hourly,
    span_days_from_hourly,
)

_LOGGER = logging.getLogger(__name__)

type EnergyShopperConfigEntry = ConfigEntry[EnergyShopperCoordinator]

STORAGE_VERSION = 1
KWH_PER_THERM = 29.3071
RATE_HISTORY_CAP = 24

# Where the stay baseline came from.
SOURCE_ENTITY = "sensor"
SOURCE_VALUE = "configured value"
SOURCE_PTC = "Price to Compare"

# Supplier sensor states that mean "on the utility's default service".
DEFAULT_SERVICE_HINTS = ("default service", "duquesne light default")


@dataclass
class Market:
    offers: list[Rate]
    price_to_compare: tuple[float, str] | None


@dataclass
class ShopperData:
    fuel: str
    decision: decision.GasDecision | decision.ElectricDecision
    unit: str
    price_to_compare: float | None  # USD per unit
    baseline_price: float | None  # what you pay now, USD per unit
    baseline_source: str | None
    offers_listed: int
    usage_count: int  # gas: monthly cycles; electric: hours
    notes: list[tuple[str, str]] = field(default_factory=list)
    current_supplier: str | None = None  # electric
    tou: TOUAnalysisResult | None = None  # electric, when Time-of-Use was scored
    tou_rates: TOURates | None = None
    updated: datetime = field(default_factory=dt_util.utcnow)

    @property
    def compared(self) -> bool:
        """
        False when no switch/stay call was made: no usage or no baseline (no stay row),
        or the market listed no offers at all, which is a site problem rather than news.
        """
        return self.offers_listed > 0 and self.decision.recommended is not None and self.decision.stay is not None

    @property
    def recommendation(self) -> str:
        if not self.compared:
            return "unknown"
        return "switch" if self.decision.recommend_switch else "stay"

    @property
    def best_offer(self) -> RankedOption | None:
        """Cheapest option that isn't what you have now (a supplier, default service or TOU)."""
        stay = self.decision.stay
        return next((o for o in self.decision.ranked if not o.is_current_egs and o is not stay), None)

    @property
    def annual_usage(self) -> float:
        d = self.decision
        return d.annual_units if isinstance(d, decision.GasDecision) else d.annual_kwh

    @property
    def tou_option(self) -> RankedOption | None:
        return next((o for o in self.decision.ranked if o.is_tou), None)


# ---------------------------------------------------------------------------
# Blocking helpers (run in the executor)
# ---------------------------------------------------------------------------


def fetch_market(distributor: str, zipcode: str, service_type: str) -> Market:
    """Gas offers and Price to Compare from PA Gas Switch. Raises on a failed offer request."""
    provider = pa.PaGasSwitch(distributor, service_type=service_type, zipcode=zipcode)
    listing = provider.fetch_offers()
    return Market(listing.offers, provider.price_to_compare())


def fetch_electric_market(distributor: str, rate_type: str, zipcode: str, service_type: str) -> Market:
    """Electric offers from PA Power Switch (it doesn't publish a Price to Compare)."""
    provider = pa.PaPowerSwitch(distributor, rate_type, service_type=service_type, zipcode=zipcode)
    return Market(provider.fetch_offers().offers, None)


def statistic_unit_kind(unit: str | None) -> str | None:
    """'volume' or 'energy' for a statistic's unit, or None if we can't convert it."""
    if unit in VolumeConverter.VALID_UNITS:
        return "volume"
    if unit in EnergyConverter.VALID_UNITS:
        return "energy"
    return None


def monthly_usage_cycles(
    rows: list[dict[str, Any]], kind: str, therms_per_mcf: float, now: datetime
) -> list[decision.UsageCycle]:
    """
    Hourly "change" rows (ft³ or kWh) → one Mcf cycle per calendar month, summed here
    rather than with the recorder's month roll-up, which drops rows starting on a DST
    change day (Nov 1 2026, say).

    The current month is dropped when the source reads daily or hourly (it's only partly
    in), but kept for a billing-cycle source like peoples_gas, which writes each complete
    cycle as one row on the 1st: there, this month's row is the newest bill.
    """
    by_month: dict[tuple[int, int], float] = defaultdict(float)
    days: dict[tuple[int, int], set] = defaultdict(set)
    for row in rows:
        change = row.get("change")
        if not change or change <= 0:
            continue
        start = dt_util.as_local(dt_util.utc_from_timestamp(row["start"]))
        key = (start.year, start.month)
        by_month[key] += change
        days[key].add(start.date())
    this_month = (now.year, now.month)
    past = sorted(len(d) for key, d in days.items() if key < this_month)
    cycle_source = bool(past) and past[len(past) // 2] <= 2  # median month has 1–2 readings
    cycles = []
    for (year, month), change in sorted(by_month.items()):
        if (year, month) > this_month or ((year, month) == this_month and not cycle_source):
            continue
        if kind == "volume":
            mcf = change / 1000.0  # ft³ → Mcf
        else:
            mcf = change / KWH_PER_THERM / therms_per_mcf  # kWh → therms → Mcf
        cycles.append(decision.UsageCycle(usage_mcf=mcf, month=month, year=year))
    return cycles


def hourly_records(rows: list[dict[str, Any]]) -> list[HourlyUsageRecord]:
    """Hourly kWh "change" rows → records with local ISO start times, as the CLI stores them."""
    out = []
    for row in rows:
        change = row.get("change")
        if not change or change <= 0:
            continue
        start = dt_util.as_local(dt_util.utc_from_timestamp(row["start"]))
        out.append(HourlyUsageRecord(dt=start.isoformat(), kwh=float(change), cost=0.0))
    return out


def tou_ready_records(records: list[HourlyUsageRecord]) -> list[HourlyUsageRecord]:
    """
    Only days that really have hourly readings. A source that backfilled older days as
    one row at midnight (core Opower, the collector) would otherwise look like all
    super-off-peak use.
    """
    by_day: dict[str, list[HourlyUsageRecord]] = defaultdict(list)
    for r in records:
        by_day[r.dt[:10]].append(r)
    return [r for day in sorted(by_day) if len(by_day[day]) >= TOU_MIN_HOURS_PER_DAY for r in by_day[day]]


def usage_window(records: list[HourlyUsageRecord], label: str) -> UsageWindow | None:
    """The CLI's usage window from hourly records (no $: the blended rate isn't needed here)."""
    if not records:
        return None
    total = sum(r.kwh for r in records)
    annual, method, month_kwh, missing = project_annual_kwh_from_hourly(records)
    return UsageWindow(
        total_kwh=total,
        span_days=span_days_from_hourly(records),
        baseline_cost=0.0,
        baseline_label=label,
        annual_kwh=annual,
        annual_method=method,
        month_kwh=month_kwh,
        missing_high_use_months=missing,
    )


def per_kwh(value: float, unit: str | None) -> float:
    """A $/kWh sensor value, accepting ¢/kWh too."""
    u = (unit or "").replace(" ", "").lower()
    if u.startswith(("¢", "c/", "cents")):
        return value / 100.0
    if u and not u.endswith("/kwh"):
        raise ValueError(f"not a per-kWh price: {unit!r}")
    return value


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class EnergyShopperCoordinator(DataUpdateCoordinator[ShopperData]):
    """Ranks one fuel's offers twice a day."""

    config_entry: EnergyShopperConfigEntry

    def __init__(self, hass: HomeAssistant, entry: EnergyShopperConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.fuel: str = entry.data.get(CONF_FUEL, FUEL_GAS)
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._state: dict[str, Any] = {}
        self._unit: str | None = None

    # -- persisted state: last recommendation, baseline price history -----------------

    async def async_load_state(self) -> None:
        self._state = await self._store.async_load() or {}
        self._unit = self._state.get("unit")

    @staticmethod
    async def async_remove_state(hass: HomeAssistant, entry_id: str) -> None:
        await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}").async_remove()

    def _opt(self, key: str, default: Any = None) -> Any:
        return self.config_entry.options.get(key, default)

    def _entity_value(self, entity_id: str | None) -> tuple[str | None, str | None]:
        """(state, unit) of an entity, or (None, None) when it has no value."""
        if not entity_id:
            return None, None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None, None
        return state.state, state.attributes.get("unit_of_measurement")

    # -- inputs ------------------------------------------------------------------------

    async def _read_statistic(
        self, notes: list, period: str, days: int, allowed: tuple[str, ...]
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Rows of the configured usage statistic (ft³ for volume, kWh for energy)."""
        statistic_id = self._opt(CONF_USAGE_STATISTIC)
        if not statistic_id:
            notes.append(("warning", "No usage statistic configured; ranking by listed price."))
            return [], None
        recorder = get_instance(self.hass)
        meta = await recorder.async_add_executor_job(
            lambda: get_metadata(self.hass, statistic_ids={statistic_id})
        )
        if statistic_id not in meta:
            notes.append(("warning", f"Usage statistic {statistic_id} not found (yet)."))
            return [], None
        unit = meta[statistic_id][1].get("unit_of_measurement")
        kind = statistic_unit_kind(unit)
        if kind not in allowed:
            what = "an energy" if allowed == ("energy",) else "a gas volume or energy"
            notes.append(("error", f"Usage statistic {statistic_id} is in {unit!r}, not {what} unit."))
            return [], None
        units = {"volume": UnitOfVolume.CUBIC_FEET} if kind == "volume" else {"energy": UnitOfEnergy.KILO_WATT_HOUR}
        start = dt_util.as_utc(dt_util.now() - timedelta(days=days))
        stats = await recorder.async_add_executor_job(
            statistics_during_period, self.hass, start, None, {statistic_id}, period, units, {"change"}
        )
        return stats.get(statistic_id, []), kind

    def _gas_baseline(
        self, unit: str, ptc: float | None, therms_per_mcf: float, notes: list
    ) -> tuple[decision.Baseline | None, str | None]:
        """Your current gas supply price in the offer unit, and where it came from."""
        entity_id = self._opt(CONF_RATE_ENTITY)
        if entity_id:
            value, value_unit = self._entity_value(entity_id)
            if value is None:
                notes.append(("warning", f"{entity_id} has no value; using the Price to Compare."))
            else:
                try:
                    price = decision.convert_gas_price(float(value), value_unit or unit, unit, therms_per_mcf)
                except ValueError as err:
                    notes.append(("error", f"Can't use {entity_id} as a gas price: {err}"))
                else:
                    return decision.Baseline(price, "Current supply rate"), SOURCE_ENTITY
        value = self._opt(CONF_RATE_VALUE)
        if value:
            price = decision.convert_gas_price(float(value), self._opt(CONF_RATE_UNIT) or unit, unit, therms_per_mcf)
            return decision.Baseline(price, "Current supply rate"), SOURCE_VALUE
        if ptc:
            return decision.Baseline(ptc), SOURCE_PTC
        notes.append(("warning", "No current rate or Price to Compare; can't say whether switching saves."))
        return None, None

    def _electric_price(self, entity_key: str, value_key: str, what: str, notes: list) -> tuple[float | None, str | None]:
        """A $/kWh price from its sensor, else its typed value."""
        entity_id = self._opt(entity_key)
        if entity_id:
            value, unit = self._entity_value(entity_id)
            if value is not None:
                try:
                    return per_kwh(float(value), unit), SOURCE_ENTITY
                except ValueError as err:
                    notes.append(("error", f"Can't use {entity_id} as the {what}: {err}"))
        typed = self._opt(value_key)
        if typed:
            return float(typed), SOURCE_VALUE
        return None, None

    def _electric_supplier(self) -> str | None:
        """Your supplier's name, or None on the utility's default service."""
        name, _ = self._entity_value(self._opt(CONF_SUPPLIER_ENTITY))
        name = (name or self._opt(CONF_SUPPLIER_NAME) or "").strip()
        if not name or any(h in name.lower() for h in DEFAULT_SERVICE_HINTS):
            return None
        return name

    def _tou_rates(self, ptc: float | None) -> TOURates | None:
        cents = [self._opt(k) for k in (CONF_TOU_PEAK, CONF_TOU_OFF_PEAK, CONF_TOU_SUPER_OFF_PEAK)]
        if not ptc or any(c in (None, "") for c in cents):
            return None
        peak, off, super_off = (float(c) / 100.0 for c in cents)
        return TOURates(
            standard_rate=ptc,
            peak_rate=peak,
            off_peak_rate=off,
            super_off_peak_rate=super_off,
            effective_start="",
            effective_end="9999-12-31",
            source="Energy Shopper settings",
        )

    # -- update ------------------------------------------------------------------------

    async def _async_update_data(self) -> ShopperData:
        shopper = await (self._update_electric() if self.fuel == FUEL_ELECTRIC else self._update_gas())
        self._sync_cheaper_issue(shopper)
        self._track_rate(shopper)
        self._fire_if_changed(shopper)
        self._state["unit"] = shopper.unit
        await self._store.async_save(self._state)
        return shopper

    async def _update_gas(self) -> ShopperData:
        data = self.config_entry.data
        try:
            market = await self.hass.async_add_executor_job(
                fetch_market,
                data[CONF_DISTRIBUTOR],
                data[CONF_ZIPCODE],
                data.get(CONF_SERVICE_TYPE, DEFAULT_SERVICE_TYPE),
            )
        except requests.RequestException as err:
            raise UpdateFailed(f"Error fetching PA Gas Switch offers: {err}") from err

        notes: list[tuple[str, str]] = []
        if not market.offers:
            notes.append(("warning", "PA Gas Switch listed no offers this time; no recommendation until it does."))
        unit = data.get(CONF_PRICE_UNIT) or (market.price_to_compare or (None, None))[1] or self._unit
        if not unit:
            raise UpdateFailed(
                "Unknown gas price unit: PA Gas Switch didn't show a Price to Compare for this "
                "ZIP and distributor. Set the price unit in the integration's settings."
            )
        self._unit = unit
        therms_per_mcf = float(self._opt(CONF_THERMS_PER_MCF) or DEFAULT_THERMS_PER_MCF)
        ptc = None
        if market.price_to_compare:
            ptc_price, ptc_unit = market.price_to_compare
            ptc = decision.convert_gas_price(ptc_price, ptc_unit, unit, therms_per_mcf)

        rows, kind = await self._read_statistic(notes, "hour", USAGE_LOOKBACK_DAYS, ("volume", "energy"))
        usage = monthly_usage_cycles(rows, kind, therms_per_mcf, dt_util.now()) if kind else []
        baseline, source = self._gas_baseline(unit, ptc, therms_per_mcf, notes)

        result = decision.decide_gas(
            market.offers,
            unit=unit,
            usage=usage,
            baseline=baseline,
            threshold_pct=float(self._opt(CONF_THRESHOLD, DEFAULT_THRESHOLD)),
            prior=parse_prior_suppliers(self._opt(CONF_PRIOR_SUPPLIERS, "") or ""),
            no_cancellation_fee=bool(self._opt(CONF_NO_CANCELLATION_FEE, False)),
            therms_per_mcf=therms_per_mcf,
            utility_name="Utility default service" if source == SOURCE_PTC else "Your current plan",
            today=dt_util.now().date(),
        )
        return ShopperData(
            fuel=FUEL_GAS,
            decision=result,
            unit=unit,
            price_to_compare=ptc,
            baseline_price=baseline.price if baseline else None,
            baseline_source=source,
            offers_listed=len(market.offers),
            usage_count=len(usage),
            notes=notes + result.notes,
        )

    async def _update_electric(self) -> ShopperData:
        data = self.config_entry.data
        try:
            market = await self.hass.async_add_executor_job(
                fetch_electric_market,
                data[CONF_DISTRIBUTOR],
                data[CONF_RATE_TYPE],
                data.get(CONF_ZIPCODE, ""),
                data.get(CONF_SERVICE_TYPE, DEFAULT_SERVICE_TYPE),
            )
        except requests.RequestException as err:
            raise UpdateFailed(f"Error fetching PA Power Switch offers: {err}") from err

        notes: list[tuple[str, str]] = []
        if not market.offers:
            notes.append(("warning", "PA Power Switch listed no offers this time; no recommendation until it does."))
        rows, kind = await self._read_statistic(notes, "hour", HOURLY_LOOKBACK_DAYS, ("energy",))
        records = hourly_records(rows) if kind else []
        usage = usage_window(records, f"hourly kWh from {self._opt(CONF_USAGE_STATISTIC)}")

        ptc, _ = self._electric_price(CONF_PTC_ENTITY, CONF_PTC_VALUE, "Price to Compare", notes)
        supplier = self._electric_supplier()
        current = None
        source = None
        if supplier:
            rate, source = self._electric_price(CONF_RATE_ENTITY, CONF_RATE_VALUE, "supply rate", notes)
            if rate:
                current = decision.CurrentPlan(
                    supplier=supplier,
                    price=rate,
                    plan_type="Current supplier",
                    flags=[],
                )
            else:
                notes.append(("warning", f"No supply rate for {supplier}; comparing against the Price to Compare."))
        if current is None and ptc:
            source = SOURCE_PTC
        if not current and not ptc:
            notes.append(("warning", "No current rate or Price to Compare; can't say whether switching saves."))

        tou_rates = self._tou_rates(ptc)
        tou_result = None
        if tou_rates is not None:
            tou_records = tou_ready_records(records)
            if tou_records:
                tou_result = await self.hass.async_add_executor_job(TOUAnalyzer().analyze, tou_records, tou_rates)
            else:
                notes.append(("warning", "Time-of-Use needs hourly usage; the statistic has none yet."))

        result = await self.hass.async_add_executor_job(
            lambda: decision.decide_electric(
                market.offers,
                usage=usage,
                current=current,
                price_to_compare=ptc,
                threshold_pct=float(self._opt(CONF_THRESHOLD, DEFAULT_THRESHOLD)),
                prior=prior_suppliers(
                    current_supplier=supplier or "",
                    configured=self._opt(CONF_PRIOR_SUPPLIERS, "") or "",
                ),
                no_cancellation_fee=bool(self._opt(CONF_NO_CANCELLATION_FEE, False)),
                renewable_only=bool(self._opt(CONF_RENEWABLE_ONLY, False)),
                tou_rates=tou_rates,
                tou_result=tou_result,
                today=dt_util.now().date(),
            )
        )
        return ShopperData(
            fuel=FUEL_ELECTRIC,
            decision=result,
            unit="kWh",
            price_to_compare=ptc,
            baseline_price=result.stay.unit_price if result.stay else None,
            baseline_source=source,
            offers_listed=len(market.offers),
            usage_count=len(records),
            notes=notes + result.all_notes,
            current_supplier=supplier,
            tou=tou_result,
            tou_rates=tou_rates,
        )

    # -- outputs: repair issues and events -----------------------------------------------

    def _our_issues(self, prefix: str) -> list[str]:
        mine = f"{prefix}_{self.config_entry.entry_id}"
        return [i for d, i in ir.async_get(self.hass).issues if d == DOMAIN and i.startswith(mine)]

    def _sync_cheaper_issue(self, shopper: ShopperData) -> None:
        """
        One issue per recommended supplier: it stays until that recommendation goes away,
        and dismissing it silences that supplier only, so a different, cheaper supplier
        raises a fresh one.
        """
        d = shopper.decision
        kind = ISSUE_CHEAPER.format(fuel=shopper.fuel)
        shop = ELECTRIC_SHOP_URL if shopper.fuel == FUEL_ELECTRIC else SHOP_URL
        keep = None
        if d.recommend_switch and d.recommended is not None:
            rec = d.recommended
            keep = f"{kind}_{self.config_entry.entry_id}_{slugify(rec.supplier + ' ' + rec.plan_type if rec.is_tou else rec.supplier)}"
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                keep,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="tou_available" if rec.is_tou else kind,
                learn_more_url=rec.more_info if rec.more_info.startswith("http") else shop,
                translation_placeholders={
                    "supplier": rec.supplier,
                    "plan": rec.plan_type or "offer",
                    "price": f"{rec.unit_price or 0:.4f}",
                    "unit": shopper.unit,
                    "savings": f"{d.savings_annual_usd or 0:,.0f}",
                    "pct": f"{d.savings_pct or 0:.0f}",
                    "baseline": f"{shopper.baseline_price or 0:.4f}",
                    "term": rec.term_raw or "unknown",
                    "phone": rec.phone or "see the listing",
                },
            )
        for issue_id in self._our_issues(kind):
            if issue_id != keep:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def _track_rate(self, shopper: ShopperData) -> None:
        """Raise an issue when your own rate (sensor or typed) rises by RATE_JUMP_PCT."""
        if shopper.baseline_source not in (SOURCE_ENTITY, SOURCE_VALUE) or not shopper.baseline_price:
            return
        history: list[dict[str, Any]] = self._state.setdefault("rate_history", [])
        price, unit = round(shopper.baseline_price, 6), shopper.unit
        last = history[-1] if history else None
        if last and last["unit"] == unit and abs(last["price"] - price) < 1e-6:
            return
        today = dt_util.now().date().isoformat()
        history.append({"price": price, "unit": unit, "date": today})
        del history[:-RATE_HISTORY_CAP]
        if not last or last["unit"] != unit or last["price"] <= 0:
            return
        pct = 100.0 * (price - last["price"]) / last["price"]
        if pct < float(self._opt(CONF_RATE_JUMP_PCT, DEFAULT_RATE_JUMP_PCT)):
            return
        kind = ISSUE_RATE_JUMP.format(fuel=shopper.fuel)
        issue_id = f"{kind}_{self.config_entry.entry_id}_{today}"
        for old in self._our_issues(kind):
            if old != issue_id:
                ir.async_delete_issue(self.hass, DOMAIN, old)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=kind,
            learn_more_url=ELECTRIC_SHOP_URL if shopper.fuel == FUEL_ELECTRIC else SHOP_URL,
            translation_placeholders={
                "pct": f"{pct:.0f}",
                "old": f"{last['price']:.4f}",
                "old_date": last["date"],
                "new": f"{price:.4f}",
                "unit": unit,
            },
        )

    def _fire_if_changed(self, shopper: ShopperData) -> None:
        """Fire EVENT_RECOMMENDATION_CHANGED when switch/stay or the recommended option changes."""
        d = shopper.decision
        rec = d.recommended
        key = shopper.recommendation
        if key == "switch" and rec is not None:
            key = f"switch:{rec.supplier}:{rec.plan_type}"
        if shopper.recommendation == "unknown":
            return  # keep the last real recommendation, so it coming back isn't news
        previous = self._state.get("recommendation_key")
        self._state["recommendation_key"] = key
        if key == previous:
            return
        if previous is None and shopper.recommendation != "switch":
            return  # first run: only worth an event if there's something to do
        self.hass.bus.async_fire(
            EVENT_RECOMMENDATION_CHANGED,
            {
                "entry_id": self.config_entry.entry_id,
                "fuel": shopper.fuel,
                "recommendation": shopper.recommendation,
                "previous": (previous or "").split(":", 1)[0] or None,
                "supplier": rec.supplier if rec else None,
                "plan_type": rec.plan_type if rec else None,
                "time_of_use": bool(rec and rec.is_tou),
                "price": rec.unit_price if rec else None,
                "unit": shopper.unit,
                "current_price": shopper.baseline_price,
                "annual_savings": d.savings_annual_usd,
                "savings_pct": d.savings_pct,
                "term": rec.term_raw if rec else None,
                "phone": rec.phone if rec else None,
                "more_info": rec.more_info if rec else None,
            },
        )
