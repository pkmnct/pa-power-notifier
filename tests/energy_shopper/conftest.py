"""Fixtures for the energy_shopper tests: a fake PA Gas Switch and a gas usage statistic."""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime
from unittest.mock import patch

import pytest
import requests

from custom_components.energy_shopper.const import (
    CONF_DISTRIBUTOR,
    CONF_SERVICE_TYPE,
    CONF_THRESHOLD,
    CONF_USAGE_STATISTIC,
    CONF_ZIPCODE,
    DOMAIN,
)
from custom_components.energy_shopper.vendor.energy_shopper_core.markets import base, pa
from custom_components.energy_shopper.vendor.energy_shopper_core.models import Rate
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

ZIP = "15222"
DISTRIBUTOR = "1234"
USAGE_ID = "peoples_gas:test_gas_consumption"
PTC = 3.57  # $/Mcf

# Typical western-PA year, Mcf per calendar month.
MONTHLY_MCF = {1: 14.0, 2: 12.0, 3: 9.0, 4: 5.0, 5: 2.5, 6: 1.5, 7: 1.2, 8: 1.2, 9: 1.6, 10: 3.5, 11: 8.0, 12: 12.0}
ANNUAL_MCF = sum(MONTHLY_MCF.values())


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """The integration depends on recorder, which must start before hass is set up."""
    yield


def offer(supplier: str, price: float, kind: str = "Fixed", term: str = "12 Months", **kw) -> Rate:
    fields = dict(
        price=f"${price:.4f}",
        supplier=supplier,
        type=kind,
        cancellation_fee=kw.pop("cancellation_fee", "None"),
        deposit_required="No",
        discounts_incentives="",
        introductory_price="",
        monthly_fee="None",
        more_info=kw.pop("more_info", ""),
        service_type="Residential",
        term_end_date="",
        term_length=term,
        utility_type="gas",
        phone=kw.pop("phone", "800-555-0100"),
    )
    fields.update(kw)
    return Rate(**fields)


class FakeMarket:
    """Mutable PA Gas Switch state; every FakeProvider made while patched reads it."""

    def __init__(self) -> None:
        self.offers = [
            offer("Steady Gas", 3.00),
            offer("Fee Gas", 2.90, cancellation_fee="$150"),
            offer("Floaty Gas", 3.40, kind="Variable", term="Month to Month"),
            offer("Welcome Gas", 2.80, new_customers_offer="Yes", phone="800-555-0199"),
        ]
        self.price_to_compare: tuple[float, str] | None = (PTC, "mcf")
        self.error: Exception | None = None
        self.requests = 0


class FakeProvider:
    def __init__(self, market: FakeMarket) -> None:
        self.market = market

    def fetch_offers(self) -> base.OfferListing:
        self.market.requests += 1
        if self.market.error is not None:
            raise self.market.error
        return base.OfferListing(list(self.market.offers), "csv")

    def price_to_compare(self):
        return self.market.price_to_compare


@pytest.fixture
def market() -> Generator[FakeMarket]:
    m = FakeMarket()
    with patch.object(pa, "PaGasSwitch", lambda *a, **kw: FakeProvider(m)):
        yield m


async def add_usage(hass: HomeAssistant, months: int = 24, unit: str = UnitOfVolume.CENTUM_CUBIC_FEET) -> None:
    """Monthly gas use as an external statistic, like the peoples_gas integration makes."""
    now = dt_util.now()
    year, month = now.year, now.month
    starts = []
    for _ in range(months + 1):  # includes the current (partial) month, which must be ignored
        starts.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    rows, total = [], 0.0
    for y, m in reversed(starts):
        ccf = MONTHLY_MCF[m] * 10 * (5 if (y, m) == (now.year, now.month) else 1)
        total += ccf
        start = dt_util.as_utc(datetime(y, m, 1, tzinfo=dt_util.get_default_time_zone()))
        rows.append({"start": start, "state": ccf, "sum": total})
    async_add_external_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": "Test gas consumption",
            "source": "peoples_gas",
            "statistic_id": USAGE_ID,
            "unit_class": VolumeConverter.UNIT_CLASS,
            "unit_of_measurement": unit,
        },
        rows,
    )
    await async_wait_recording_done(hass)


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Gas offers {ZIP}",
        unique_id=f"gas_{DISTRIBUTOR}_{ZIP}_residential",
        data={CONF_ZIPCODE: ZIP, CONF_DISTRIBUTOR: DISTRIBUTOR, CONF_SERVICE_TYPE: "residential"},
        options={CONF_USAGE_STATISTIC: USAGE_ID, CONF_THRESHOLD: 5.0},
    )


CONNECTION_ERROR = requests.ConnectionError("down")
