#!/usr/bin/env python3
"""
Gas unit conversion and forecasting for the standalone notifier: energy_shopper_core.gas
with the heat content taken from the environment.

Env:
  THERMS_PER_MCF — heat content of your gas (default 10.37, typical for Pennsylvania;
                   your bill may list a BTU factor).
"""

from __future__ import annotations

import os
from typing import Any, List

from energy_shopper_core import gas as _gas
from energy_shopper_core.gas import (  # noqa: F401  (re-exported)
    DEFAULT_THERMS_PER_MCF,
    GAS_PRICE_UNITS,
    ForecastResult,
    build_calendar_month_profile,
    forecast_simple_average_annual_mcf,
    parse_money_field,
)


def therms_per_mcf() -> float:
    """Therms per MCF used to align supplier $/therm with utility MCF usage."""
    try:
        return float(os.environ.get("THERMS_PER_MCF", str(DEFAULT_THERMS_PER_MCF)))
    except ValueError:
        return DEFAULT_THERMS_PER_MCF


def mcf_to_therms(mcf: float) -> float:
    return _gas.mcf_to_therms(mcf, therms_per_mcf())


def usd_per_mcf_to_usd_per_therm(usd_per_mcf: float) -> float:
    return _gas.usd_per_mcf_to_usd_per_therm(usd_per_mcf, therms_per_mcf())


def therms_per_gas_unit(unit: str) -> float:
    """Therms in one PA Gas Switch price unit ('mcf', 'ccf' = 0.1 Mcf, or 'therm')."""
    return _gas.therms_per_gas_unit(unit, therms_per_mcf())


def forecast_annual_from_calendar_profile(
    records: List[Any], *, aggregator: str = "median"
) -> ForecastResult:
    return _gas.forecast_annual_from_calendar_profile(
        records, aggregator=aggregator, therms_per_mcf=therms_per_mcf()
    )
