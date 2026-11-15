"""
Coordinator for Energy Shopper (gas).

Twice a day: fetch PA Gas Switch offers and the Price to Compare, read your usage from
a long-term statistic and your current rate from a sensor (or a typed value), and rank
the offers with energy_shopper_core.decision.decide_gas, the same function the
standalone notifier uses. Then keep the "cheaper supplier" and "rate jump" repair
issues in step and fire an event when the recommendation changes.
"""

from __future__ import annotations

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
    CONF_NO_CANCELLATION_FEE,
    CONF_PRICE_UNIT,
    CONF_PRIOR_SUPPLIERS,
    CONF_RATE_ENTITY,
    CONF_RATE_JUMP_PCT,
    CONF_RATE_UNIT,
    CONF_RATE_VALUE,
    CONF_SERVICE_TYPE,
    CONF_THERMS_PER_MCF,
    CONF_THRESHOLD,
    CONF_USAGE_STATISTIC,
    CONF_ZIPCODE,
    DEFAULT_RATE_JUMP_PCT,
    DEFAULT_SERVICE_TYPE,
    DEFAULT_THRESHOLD,
    DOMAIN,
    EVENT_RECOMMENDATION_CHANGED,
    ISSUE_CHEAPER_GAS,
    ISSUE_RATE_JUMP,
    SHOP_URL,
    UPDATE_INTERVAL,
    USAGE_LOOKBACK_DAYS,
)
from .vendor.energy_shopper_core import decision
from .vendor.energy_shopper_core.eligibility import parse_prior_suppliers
from .vendor.energy_shopper_core.gas import DEFAULT_THERMS_PER_MCF
from .vendor.energy_shopper_core.markets import pa
from .vendor.energy_shopper_core.models import Rate
from .vendor.energy_shopper_core.report import RankedOption

_LOGGER = logging.getLogger(__name__)

type EnergyShopperConfigEntry = ConfigEntry[EnergyShopperCoordinator]

STORAGE_VERSION = 1
KWH_PER_THERM = 29.3071
RATE_HISTORY_CAP = 24

# Where the stay baseline came from.
SOURCE_ENTITY = "sensor"
SOURCE_VALUE = "configured value"
SOURCE_PTC = "Price to Compare"


@dataclass
class Market:
    offers: list[Rate]
    price_to_compare: tuple[float, str] | None


@dataclass
class GasShopperData:
    decision: decision.GasDecision
    unit: str
    price_to_compare: float | None  # USD per unit, from PA Gas Switch
    baseline_price: float | None
    baseline_source: str | None
    offers_listed: int
    usage_cycles: int
    notes: list[tuple[str, str]] = field(default_factory=list)
    updated: datetime = field(default_factory=dt_util.utcnow)

    @property
    def recommendation(self) -> str:
        if self.decision.recommended is None:
            return "unknown"
        return "switch" if self.decision.recommend_switch else "stay"

    @property
    def best_offer(self) -> RankedOption | None:
        """Cheapest offer from a supplier (not the stay baseline)."""
        return next((o for o in self.decision.ranked if not o.is_current_egs), None)


# ---------------------------------------------------------------------------
# Blocking helpers (run in the executor)
# ---------------------------------------------------------------------------


def fetch_market(distributor: str, zipcode: str, service_type: str) -> Market:
    """Offers and Price to Compare from PA Gas Switch. Raises on a failed offer request."""
    provider = pa.PaGasSwitch(distributor, service_type=service_type, zipcode=zipcode)
    listing = provider.fetch_offers()
    return Market(listing.offers, provider.price_to_compare())


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
    Monthly "change" rows (ft³ or kWh) → Mcf cycles. The current calendar month is
    skipped: for daily or hourly sources it's only partly in.
    """
    this_month = (now.year, now.month)
    cycles = []
    for row in rows:
        change = row.get("change")
        if not change or change <= 0:
            continue
        start = dt_util.as_local(dt_util.utc_from_timestamp(row["start"]))
        if (start.year, start.month) >= this_month:
            continue
        if kind == "volume":
            mcf = change / 1000.0  # ft³ → Mcf
        else:
            mcf = change / KWH_PER_THERM / therms_per_mcf  # kWh → therms → Mcf
        cycles.append(decision.UsageCycle(usage_mcf=mcf, month=start.month, year=start.year))
    return cycles


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class EnergyShopperCoordinator(DataUpdateCoordinator[GasShopperData]):
    """Ranks gas offers twice a day."""

    config_entry: EnergyShopperConfigEntry

    def __init__(self, hass: HomeAssistant, entry: EnergyShopperConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
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

    # -- inputs ------------------------------------------------------------------------

    async def _read_usage(self, therms_per_mcf: float, notes: list) -> list[decision.UsageCycle]:
        statistic_id = self._opt(CONF_USAGE_STATISTIC)
        if not statistic_id:
            notes.append(("warning", "No usage statistic configured; ranking by listed price."))
            return []
        recorder = get_instance(self.hass)
        meta = await recorder.async_add_executor_job(
            lambda: get_metadata(self.hass, statistic_ids={statistic_id})
        )
        if statistic_id not in meta:
            notes.append(("warning", f"Usage statistic {statistic_id} not found (yet)."))
            return []
        unit = meta[statistic_id][1].get("unit_of_measurement")
        kind = statistic_unit_kind(unit)
        if kind is None:
            notes.append(("error", f"Usage statistic {statistic_id} is in {unit!r}, not a gas volume or energy unit."))
            return []
        units = {"volume": UnitOfVolume.CUBIC_FEET} if kind == "volume" else {"energy": UnitOfEnergy.KILO_WATT_HOUR}
        now = dt_util.now()
        start = dt_util.as_utc(now - timedelta(days=USAGE_LOOKBACK_DAYS))
        stats = await recorder.async_add_executor_job(
            statistics_during_period, self.hass, start, None, {statistic_id}, "month", units, {"change"}
        )
        return monthly_usage_cycles(stats.get(statistic_id, []), kind, therms_per_mcf, now)

    def _baseline(
        self, unit: str, ptc: float | None, therms_per_mcf: float, notes: list
    ) -> tuple[decision.Baseline | None, str | None]:
        """Your current supply price in the offer unit, and where it came from."""
        entity_id = self._opt(CONF_RATE_ENTITY)
        if entity_id:
            state = self.hass.states.get(entity_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                notes.append(("warning", f"{entity_id} has no value; using the Price to Compare."))
            else:
                try:
                    price = decision.convert_gas_price(
                        float(state.state),
                        state.attributes.get("unit_of_measurement", "") or unit,
                        unit,
                        therms_per_mcf,
                    )
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

    # -- update ------------------------------------------------------------------------

    async def _async_update_data(self) -> GasShopperData:
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

        usage = await self._read_usage(therms_per_mcf, notes)
        baseline, source = self._baseline(unit, ptc, therms_per_mcf, notes)

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
        shopper = GasShopperData(
            decision=result,
            unit=unit,
            price_to_compare=ptc,
            baseline_price=baseline.price if baseline else None,
            baseline_source=source,
            offers_listed=len(market.offers),
            usage_cycles=len(usage),
            notes=notes + result.notes,
        )
        self._sync_cheaper_issue(shopper)
        self._track_rate(shopper)
        self._fire_if_changed(shopper)
        self._state["unit"] = unit
        await self._store.async_save(self._state)
        return shopper

    # -- outputs: repair issues and events -----------------------------------------------

    def _our_issues(self, prefix: str) -> list[str]:
        mine = f"{prefix}_{self.config_entry.entry_id}"
        return [i for d, i in ir.async_get(self.hass).issues if d == DOMAIN and i.startswith(mine)]

    def _sync_cheaper_issue(self, shopper: GasShopperData) -> None:
        """
        One issue per recommended supplier: it stays until that recommendation goes away,
        and dismissing it silences that supplier only, so a different, cheaper supplier
        raises a fresh one.
        """
        d = shopper.decision
        keep = None
        if d.recommend_switch and d.recommended is not None:
            rec = d.recommended
            keep = f"{ISSUE_CHEAPER_GAS}_{self.config_entry.entry_id}_{slugify(rec.supplier)}"
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                keep,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_CHEAPER_GAS,
                learn_more_url=rec.more_info if rec.more_info.startswith("http") else SHOP_URL,
                translation_placeholders={
                    "supplier": rec.supplier,
                    "plan": rec.plan_type or "offer",
                    "price": f"{rec.unit_price:.4f}",
                    "unit": shopper.unit,
                    "savings": f"{d.savings_annual_usd or 0:,.0f}",
                    "pct": f"{d.savings_pct or 0:.0f}",
                    "baseline": f"{shopper.baseline_price or 0:.4f}",
                    "term": rec.term_raw or "unknown",
                    "phone": rec.phone or "see the PA Gas Switch listing",
                },
            )
        for issue_id in self._our_issues(ISSUE_CHEAPER_GAS):
            if issue_id != keep:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def _track_rate(self, shopper: GasShopperData) -> None:
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
        issue_id = f"{ISSUE_RATE_JUMP}_{self.config_entry.entry_id}_{today}"
        for old in self._our_issues(ISSUE_RATE_JUMP):
            if old != issue_id:
                ir.async_delete_issue(self.hass, DOMAIN, old)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_RATE_JUMP,
            learn_more_url=SHOP_URL,
            translation_placeholders={
                "pct": f"{pct:.0f}",
                "old": f"{last['price']:.4f}",
                "old_date": last["date"],
                "new": f"{price:.4f}",
                "unit": unit,
            },
        )

    def _fire_if_changed(self, shopper: GasShopperData) -> None:
        """Fire EVENT_RECOMMENDATION_CHANGED when switch/stay or the supplier changes."""
        d = shopper.decision
        rec = d.recommended
        key = shopper.recommendation
        if key == "switch" and rec is not None:
            key = f"switch:{rec.supplier}:{rec.plan_type}"
        previous = self._state.get("recommendation_key")
        self._state["recommendation_key"] = key
        if key == previous or shopper.recommendation == "unknown":
            return
        if previous is None and shopper.recommendation != "switch":
            return  # first run: only worth an event if there's something to do
        self.hass.bus.async_fire(
            EVENT_RECOMMENDATION_CHANGED,
            {
                "entry_id": self.config_entry.entry_id,
                "fuel": "gas",
                "recommendation": shopper.recommendation,
                "previous": (previous or "").split(":", 1)[0] or None,
                "supplier": rec.supplier if rec else None,
                "plan_type": rec.plan_type if rec else None,
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
