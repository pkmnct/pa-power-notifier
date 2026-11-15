"""Sensors for Energy Shopper (gas): recommendation, best offer, savings, rates, leaderboard."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CURRENCY, DOMAIN, LEADERBOARD_SIZE, SHOP_URL
from .coordinator import EnergyShopperConfigEntry, EnergyShopperCoordinator, GasShopperData
from .vendor.energy_shopper_core.report import RankedOption

RECOMMENDATIONS = ["switch", "stay", "unknown"]


def _offer_attrs(o: RankedOption | None) -> dict[str, Any]:
    if o is None:
        return {}
    return {
        "supplier": o.supplier,
        "plan_type": o.plan_type,
        "price": o.unit_price,
        "unit": o.unit,
        "annual_supply_usd": o.annual_supply_usd,
        "monthly_fee": o.monthly_fee,
        "term": o.term_raw,
        "term_months": o.term_months,
        "cancellation_fee": o.cancellation_fee or None,
        "introductory_price": o.intro_raw or None,
        "new_customers_only": o.new_customers_only,
        "phone": o.phone or None,
        "more_info": o.more_info or None,
        "flags": o.flags,
    }


def _row(o: RankedOption) -> dict[str, Any]:
    return {
        "supplier": o.supplier,
        "plan_type": o.plan_type,
        "price": o.unit_price,
        "annual_supply_usd": o.annual_supply_usd,
        "term": o.term_raw,
        "current": o.is_current_egs,
        "new_customers_only": o.new_customers_only,
        "phone": o.phone or None,
    }


def _recommendation_attrs(data: GasShopperData) -> dict[str, Any]:
    d = data.decision
    return {
        "recommended": _offer_attrs(d.recommended),
        "lock_in_note": d.lock_in_note,
        "usage_summary": d.usage_summary or None,
        "usage_months": data.usage_cycles,
        "baseline_source": data.baseline_source,
        "notes": [f"{level}: {msg}" for level, msg in data.notes],
        "last_checked": data.updated.isoformat(),
    }


def _leaderboard_attrs(data: GasShopperData) -> dict[str, Any]:
    d = data.decision
    return {
        "unit": data.unit,
        "offers_listed": data.offers_listed,
        "offers_after_filters": d.offers_after_filters,
        "leaderboard": [_row(o) for o in d.ranked[:LEADERBOARD_SIZE]],
        "best_by_category": {label: _row(o) for label, o in d.highlights},
    }


@dataclass(frozen=True, kw_only=True)
class ShopperSensorDescription(SensorEntityDescription):
    value_fn: Callable[[GasShopperData], Any]
    attrs_fn: Callable[[GasShopperData], dict[str, Any]] | None = None
    price: bool = False  # unit is USD per the offer unit, known only at runtime


SENSORS: tuple[ShopperSensorDescription, ...] = (
    ShopperSensorDescription(
        key="gas_recommendation",
        translation_key="gas_recommendation",
        device_class=SensorDeviceClass.ENUM,
        options=RECOMMENDATIONS,
        value_fn=lambda data: data.recommendation,
        attrs_fn=_recommendation_attrs,
    ),
    ShopperSensorDescription(
        key="gas_best_offer",
        translation_key="gas_best_offer",
        value_fn=lambda data: data.best_offer.supplier if data.best_offer else None,
        attrs_fn=lambda data: _offer_attrs(data.best_offer),
    ),
    ShopperSensorDescription(
        key="gas_best_offer_price",
        translation_key="gas_best_offer_price",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        price=True,
        value_fn=lambda data: data.best_offer.unit_price if data.best_offer else None,
    ),
    ShopperSensorDescription(
        key="gas_annual_savings",
        translation_key="gas_annual_savings",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=0,
        value_fn=lambda data: (
            round(data.decision.savings_annual_usd, 2)
            if data.decision.savings_annual_usd is not None
            else None
        ),
    ),
    ShopperSensorDescription(
        key="gas_savings_percent",
        translation_key="gas_savings_percent",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: (
            round(data.decision.savings_pct, 2) if data.decision.savings_pct is not None else None
        ),
    ),
    ShopperSensorDescription(
        key="gas_current_rate",
        translation_key="gas_current_rate",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        price=True,
        value_fn=lambda data: data.baseline_price,
        attrs_fn=lambda data: {"source": data.baseline_source},
    ),
    ShopperSensorDescription(
        key="gas_price_to_compare",
        translation_key="gas_price_to_compare",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        price=True,
        value_fn=lambda data: data.price_to_compare,
    ),
    ShopperSensorDescription(
        key="gas_annual_usage",
        translation_key="gas_annual_usage",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: round(data.decision.annual_units, 2) or None,
        attrs_fn=lambda data: {"annual_therms": round(data.decision.annual_therms, 1)},
    ),
    ShopperSensorDescription(
        key="gas_offers",
        translation_key="gas_offers",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: sum(1 for o in data.decision.ranked if not o.is_current_egs),
        attrs_fn=_leaderboard_attrs,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyShopperConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(ShopperSensor(coordinator, d) for d in SENSORS)


class ShopperSensor(CoordinatorEntity[EnergyShopperCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: ShopperSensorDescription

    def __init__(self, coordinator: EnergyShopperCoordinator, description: ShopperSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="PA Gas Switch",
            model="Gas supplier offers",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=SHOP_URL,
        )

    @property
    def native_unit_of_measurement(self) -> str | None:
        unit = self.coordinator.data.unit if self.coordinator.data else None
        if self.entity_description.price:
            return f"{CURRENCY}/{unit}" if unit else None
        if self.entity_description.key == "gas_annual_usage":
            return unit
        return self.entity_description.native_unit_of_measurement

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
