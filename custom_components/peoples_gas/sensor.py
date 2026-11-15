"""Sensors for Peoples Gas: rates from the newest bill, bill amount and date, balance."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CURRENCY, DOMAIN, MANUFACTURER, PORTAL_URL
from .coordinator import PeoplesGasConfigEntry, PeoplesGasCoordinator, PeoplesGasData
from .vendor.energy_shopper_core import gas

PRICE_UNIT = f"{CURRENCY}/{UnitOfVolume.CENTUM_CUBIC_FEET}"


def _per_ccf(per_mcf: float | None) -> float | None:
    return None if per_mcf is None else round(gas.usd_per_mcf_to_usd_per_ccf(per_mcf), 5)


def _rate_attrs(key: str) -> Callable[[PeoplesGasData], dict[str, Any]]:
    def attrs(data: PeoplesGasData) -> dict[str, Any]:
        bill = data.latest_bill
        if bill is None:
            return {}
        return {
            "per_mcf": getattr(bill, key),
            "bill_date": bill.bill_date.isoformat(),
            "invoice": bill.invoice,
            "supplier": bill.supplier or None,
        }

    return attrs


@dataclass(frozen=True, kw_only=True)
class PeoplesGasSensorDescription(SensorEntityDescription):
    value_fn: Callable[[PeoplesGasData], float | date | None]
    attrs_fn: Callable[[PeoplesGasData], dict[str, Any]] | None = None


SENSORS: tuple[PeoplesGasSensorDescription, ...] = (
    PeoplesGasSensorDescription(
        key="price_to_compare",
        translation_key="price_to_compare",
        native_unit_of_measurement=PRICE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda d: _per_ccf(d.latest_bill and d.latest_bill.price_to_compare_per_mcf),
        attrs_fn=_rate_attrs("price_to_compare_per_mcf"),
    ),
    PeoplesGasSensorDescription(
        key="commodity_rate",
        translation_key="commodity_rate",
        native_unit_of_measurement=PRICE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda d: _per_ccf(d.latest_bill and d.latest_bill.commodity_per_mcf),
        attrs_fn=_rate_attrs("commodity_per_mcf"),
    ),
    PeoplesGasSensorDescription(
        key="last_bill_amount",
        translation_key="last_bill_amount",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value_fn=lambda d: d.latest_bill.amount if d.latest_bill else None,
        attrs_fn=lambda d: (
            {"invoice": d.latest_bill.invoice, **d.latest_bill.details} if d.latest_bill else {}
        ),
    ),
    PeoplesGasSensorDescription(
        key="last_bill_date",
        translation_key="last_bill_date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda d: d.latest_bill.bill_date if d.latest_bill else None,
    ),
    PeoplesGasSensorDescription(
        key="current_balance",
        translation_key="current_balance",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value_fn=lambda d: d.balance,
        attrs_fn=lambda d: {"due_date": d.balance_due.isoformat() if d.balance_due else None},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PeoplesGasConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(PeoplesGasSensor(coordinator, desc) for desc in SENSORS)


class PeoplesGasSensor(CoordinatorEntity[PeoplesGasCoordinator], SensorEntity):
    """A value from the Peoples Gas portal."""

    _attr_has_entity_name = True
    entity_description: PeoplesGasSensorDescription

    def __init__(
        self, coordinator: PeoplesGasCoordinator, description: PeoplesGasSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        account = coordinator.config_entry.unique_id or coordinator.config_entry.entry_id
        self._attr_unique_id = f"{account}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, account)},
            name=f"Peoples Gas {account}",
            manufacturer=MANUFACTURER,
            model="Gas account",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=PORTAL_URL,
        )

    @property
    def native_value(self) -> float | date | None:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
