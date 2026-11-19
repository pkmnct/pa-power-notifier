"""Sensors for Duquesne Light (collector): supplier and rates from the newest bill, collector health."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CURRENCY, DEFAULT_SERVICE, DOMAIN, MANUFACTURER
from .coordinator import CollectorConfigEntry, CollectorCoordinator, CollectorData

PRICE_UNIT = f"{CURRENCY}/{UnitOfEnergy.KILO_WATT_HOUR}"


def _bill(key: str) -> Callable[[CollectorData], Any]:
    return lambda data: (data.bill or {}).get(key)


def _bill_date(data: CollectorData) -> date | None:
    value = (data.bill or {}).get("date")
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _supplier(data: CollectorData) -> str | None:
    if data.bill is None:
        return None
    return data.bill.get("supplier") or DEFAULT_SERVICE


def _bill_attrs(data: CollectorData) -> dict[str, Any]:
    bill = data.bill or {}
    return {
        "default_service": bill.get("default_service"),
        "bill_date": bill.get("date"),
        "bill_id": bill.get("bill_id"),
        "rate_schedule": bill.get("rate_schedule"),
    }


def _status_attrs(data: CollectorData) -> dict[str, Any]:
    s = data.status
    return {
        "version": s.get("version"),
        "next_run": s.get("next_run"),
        "running": s.get("running"),
        "last_error": s.get("last_error"),
        "last_run": s.get("last_run"),
        "archive": s.get("archive"),
        "bills_archived": data.bills,
    }


@dataclass(frozen=True, kw_only=True)
class CollectorSensorDescription(SensorEntityDescription):
    value_fn: Callable[[CollectorData], float | str | date | datetime | None]
    attrs_fn: Callable[[CollectorData], dict[str, Any]] | None = None


SENSORS: tuple[CollectorSensorDescription, ...] = (
    CollectorSensorDescription(
        key="supplier",
        translation_key="supplier",
        value_fn=_supplier,
        attrs_fn=_bill_attrs,
    ),
    CollectorSensorDescription(
        key="supply_rate",
        translation_key="supply_rate",
        native_unit_of_measurement=PRICE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=_bill("supply_rate_per_kwh"),
        attrs_fn=_bill_attrs,
    ),
    CollectorSensorDescription(
        key="price_to_compare",
        translation_key="price_to_compare",
        native_unit_of_measurement=PRICE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=_bill("price_to_compare_per_kwh"),
    ),
    CollectorSensorDescription(
        key="last_bill_amount",
        translation_key="last_bill_amount",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value_fn=_bill("amount_due"),
    ),
    CollectorSensorDescription(
        key="last_bill_date",
        translation_key="last_bill_date",
        device_class=SensorDeviceClass.DATE,
        value_fn=_bill_date,
    ),
    CollectorSensorDescription(
        key="last_bill_usage",
        translation_key="last_bill_usage",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=0,
        value_fn=_bill("kwh"),
    ),
    CollectorSensorDescription(
        key="last_bill_supply_charges",
        translation_key="last_bill_supply_charges",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value_fn=_bill("supply_charges"),
    ),
    CollectorSensorDescription(
        key="last_success",
        translation_key="last_success",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.last_success,
        attrs_fn=_status_attrs,
    ),
    CollectorSensorDescription(
        key="usage_through",
        translation_key="usage_through",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.usage_through,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CollectorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(CollectorSensor(entry.runtime_data, d) for d in SENSORS)


class CollectorSensor(CoordinatorEntity[CollectorCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: CollectorSensorDescription

    def __init__(self, coordinator: CollectorCoordinator, description: CollectorSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=MANUFACTURER,
            model="Duquesne collector",
            sw_version=(coordinator.data.status.get("version") if coordinator.data else None),
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=coordinator.client.url + "/status",
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
