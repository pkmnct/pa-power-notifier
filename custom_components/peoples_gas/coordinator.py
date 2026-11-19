"""Coordinator for Peoples Gas: polls the portal and imports usage as statistics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import logging
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

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
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import slugify
from homeassistant.util.unit_conversion import VolumeConverter

from .const import (
    BILL_PDF_DIR,
    CONF_SAVE_BILL_PDFS,
    DEFAULT_SAVE_BILL_PDFS,
    DOMAIN,
    UPDATE_INTERVAL,
    UTILITY_TIME_ZONE,
)
from .vendor.energy_shopper_core import gas
from .vendor.energy_shopper_core.utilities import peoples_gas as pg

_LOGGER = logging.getLogger(__name__)

type PeoplesGasConfigEntry = ConfigEntry[PeoplesGasCoordinator]


@dataclass
class LatestBill:
    """The newest bill, with rates read from its PDF when it could be parsed."""

    invoice: str
    bill_date: date
    amount: float | None
    price_to_compare_per_mcf: float | None = None
    commodity_per_mcf: float | None = None
    supplier: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class PeoplesGasData:
    """One poll of the portal."""

    account: str
    usage: list[pg.GasUsageData]
    balance: float | None = None
    balance_due: date | None = None
    latest_bill: LatestBill | None = None
    bills_saved: int = 0


# ---------------------------------------------------------------------------
# Blocking portal access (runs in the executor)
# ---------------------------------------------------------------------------


def _parse_mdy(value: str | None) -> date | None:
    try:
        return datetime.strptime(str(value or "").strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def parse_balance(raw: dict) -> tuple[float | None, date | None]:
    """Balance and due date from LoadBilling: {"d": {"ToTalBalance": "193.7600", "BillDue": "05/12/2026"}}."""
    d = pg._normalize_aspnet_d(raw.get("d") if isinstance(raw, dict) else None)  # noqa: SLF001
    balance = d.get("ToTalBalance", d.get("TotalBalance"))
    return pg.parse_amount(balance) if balance not in (None, "") else None, _parse_mdy(
        d.get("BillDue")
    )


def bill_pdf_name(row: dict) -> str:
    return f"{(row.get('DBDate') or '')[:10]}_{row.get('InvoiceNumber')}.pdf"


def _newest_bill_row(rows: list[dict]) -> dict | None:
    return max(rows, key=lambda r: r.get("DBDate") or "", default=None)


def fetch_portal_data(
    username: str,
    password: str,
    previous_bill: LatestBill | None,
    pdf_dir: Path | None,
) -> PeoplesGasData:
    """Log in and fetch usage, bills and balance. Re-reads the newest bill's PDF only when it changed."""
    client = pg.PeoplesGasClient(username, password)
    client.login()
    account = client._account_number() or username.strip().lower()  # noqa: SLF001

    usage = pg.fetch_all_usage(client)
    raw_bills = client.fetch_billing_history()
    usage = pg.cross_reference(usage, pg.parse_billing_history(raw_bills))
    rows = pg.bill_rows(raw_bills)
    balance, balance_due = parse_balance(client.fetch_current_balance())

    pdfs: dict[str, bytes] = {}
    saved = 0
    if pdf_dir is not None:
        pdf_dir.mkdir(parents=True, exist_ok=True)
        for row in rows:
            path = pdf_dir / bill_pdf_name(row)
            if path.exists():
                continue
            pdf = client.fetch_bill_pdf(row)
            if pdf:
                path.write_bytes(pdf)
                pdfs[str(row["InvoiceNumber"])] = pdf
                saved += 1
        if saved:
            _LOGGER.info("Saved %d Peoples Gas bill PDFs to %s", saved, pdf_dir)

    latest = None
    newest = _newest_bill_row(rows)
    if newest is not None:
        invoice = str(newest["InvoiceNumber"])
        if previous_bill is not None and previous_bill.invoice == invoice:
            latest = previous_bill
        else:
            latest = LatestBill(
                invoice=invoice,
                bill_date=datetime.strptime(newest["DBDate"][:10], "%Y-%m-%d").date(),
                amount=pg.parse_amount(newest.get("Amount")),
            )
            pdf = pdfs.get(invoice)
            if pdf is None and pdf_dir is not None and (pdf_dir / bill_pdf_name(newest)).exists():
                pdf = (pdf_dir / bill_pdf_name(newest)).read_bytes()
            if pdf is None:
                pdf = client.fetch_bill_pdf(newest)
            if pdf:
                details = pg.parse_bill_pdf_text(pg.pdf_text(pdf))
                latest.supplier = details.pop("supplier", "") or ""
                latest.price_to_compare_per_mcf = details.get("price_to_compare_per_mcf")
                latest.commodity_per_mcf = details.get("commodity_per_mcf")
                latest.details = details

    return PeoplesGasData(
        account=account,
        usage=usage,
        balance=balance,
        balance_due=balance_due,
        latest_bill=latest,
        bills_saved=saved,
    )


# ---------------------------------------------------------------------------
# Statistics mapping (pure)
# ---------------------------------------------------------------------------


def cycle_start(record: pg.GasUsageData, tz: ZoneInfo) -> datetime | None:
    """
    The hour a billing cycle's usage is recorded at: local midnight of the cycle's read date.

    The portal labels each cycle by its billing month and dates the read on the 1st of that
    month (ToDate "5/1/2025" for the bill issued in May), so usage lands in the month it was
    billed. Records without a usable date are skipped.
    """
    read = _parse_mdy(record.to_date) or _parse_mdy(record.date)
    if read is None and record.year and record.month:
        read = date(int(record.year), int(record.month), 1)
    if read is None:
        return None
    return datetime(read.year, read.month, read.day, tzinfo=tz)


def build_statistics(
    usage: list[pg.GasUsageData],
    tz: ZoneInfo,
    last_start: float | None,
    last_consumption_sum: float,
    last_cost_start: float | None,
    last_cost_sum: float,
) -> tuple[list[StatisticData], list[StatisticData]]:
    """
    Consumption (CCF) and cost (USD) rows for cycles newer than what's already stored.

    last_start / last_cost_start are the newest stored row's start (a UTC timestamp) or
    None on first run, when every cycle is imported. Sums continue from the stored sums.
    """
    cycles = sorted(
        ((start, rec) for rec in usage if (start := cycle_start(rec, tz)) is not None),
        key=lambda item: item[0],
    )
    consumption: list[StatisticData] = []
    cost: list[StatisticData] = []
    usage_sum, cost_sum = last_consumption_sum, last_cost_sum
    seen: set[datetime] = set()
    for start, rec in cycles:
        if start in seen:  # two records for one read date: keep the first (current data)
            continue
        seen.add(start)
        ts = start.timestamp()
        if last_start is None or ts > last_start:
            ccf = gas.mcf_to_ccf(rec.usage_mcf)
            usage_sum += ccf
            consumption.append(StatisticData(start=start, state=ccf, sum=usage_sum))
        if rec.bill_amount is not None and (last_cost_start is None or ts > last_cost_start):
            cost_sum += rec.bill_amount
            cost.append(StatisticData(start=start, state=rec.bill_amount, sum=cost_sum))
    return consumption, cost


def statistic_ids(account: str) -> tuple[str, str]:
    prefix = f"{DOMAIN}:{slugify(account)}"
    return f"{prefix}_gas_consumption", f"{prefix}_gas_cost"


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class PeoplesGasCoordinator(DataUpdateCoordinator[PeoplesGasData]):
    """Polls peopleseaccount.com twice a day."""

    config_entry: PeoplesGasConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PeoplesGasConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )

    @property
    def pdf_dir(self) -> Path | None:
        if not self.config_entry.options.get(CONF_SAVE_BILL_PDFS, DEFAULT_SAVE_BILL_PDFS):
            return None
        return Path(self.hass.config.path(BILL_PDF_DIR, slugify(self.config_entry.unique_id or "account")))

    async def _async_update_data(self) -> PeoplesGasData:
        previous = self.data.latest_bill if self.data else None
        try:
            data = await self.hass.async_add_executor_job(
                fetch_portal_data,
                self.config_entry.data[CONF_USERNAME],
                self.config_entry.data[CONF_PASSWORD],
                previous,
                self.pdf_dir,
            )
        except pg.PeoplesGasAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (requests.RequestException, ValueError, RuntimeError, OSError) as err:
            raise UpdateFailed(f"Error fetching Peoples Gas data: {err}") from err

        await self._insert_statistics(data)
        return data

    async def _insert_statistics(self, data: PeoplesGasData) -> None:
        """Import new billing cycles as external statistics (backfills on first run)."""
        # The account captured at setup, not this poll's lookup: one bad page load (the
        # lookup falls back to the username) must not start a second statistic series.
        account = self.config_entry.unique_id or data.account
        consumption_id, cost_id = statistic_ids(account)
        tz = ZoneInfo(UTILITY_TIME_ZONE)
        recorder = get_instance(self.hass)

        async def last(statistic_id: str) -> tuple[float | None, float]:
            stat = await recorder.async_add_executor_job(
                get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
            )
            if not stat:
                return None, 0.0
            row = stat[statistic_id][0]
            return row["start"], row.get("sum") or 0.0

        last_start, last_sum = await last(consumption_id)
        last_cost_start, last_cost_sum = await last(cost_id)
        if last_start is None:
            _LOGGER.debug("No Peoples Gas statistics yet; backfilling all cycles")

        consumption, cost = build_statistics(
            data.usage, tz, last_start, last_sum, last_cost_start, last_cost_sum
        )
        name = f"Peoples Gas {account}"
        if consumption:
            async_add_external_statistics(
                self.hass,
                StatisticMetaData(
                    mean_type=StatisticMeanType.NONE,
                    has_sum=True,
                    name=f"{name} gas consumption",
                    source=DOMAIN,
                    statistic_id=consumption_id,
                    unit_class=VolumeConverter.UNIT_CLASS,
                    unit_of_measurement=UnitOfVolume.CENTUM_CUBIC_FEET,
                ),
                consumption,
            )
        if cost:
            async_add_external_statistics(
                self.hass,
                StatisticMetaData(
                    mean_type=StatisticMeanType.NONE,
                    has_sum=True,
                    name=f"{name} gas cost",
                    source=DOMAIN,
                    statistic_id=cost_id,
                    unit_class=None,
                    unit_of_measurement=None,  # as core Opower does for cost
                ),
                cost,
            )
        _LOGGER.debug(
            "Imported %d consumption and %d cost rows for %s",
            len(consumption),
            len(cost),
            account,
        )
