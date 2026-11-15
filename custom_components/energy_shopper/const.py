"""Constants for the Energy Shopper integration (gas, Pennsylvania)."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "energy_shopper"

# PA Gas Switch publishes offer changes during the day; twice a day is plenty and polite.
UPDATE_INTERVAL = timedelta(hours=12)

# Config entry data: which market to shop.
CONF_ZIPCODE = "zipcode"
CONF_DISTRIBUTOR = "distributor"
CONF_SERVICE_TYPE = "service_type"
CONF_PRICE_UNIT = "price_unit"  # "" = read it from the PA Gas Switch shop page

# Options: your usage, your current rate, and how picky to be.
CONF_USAGE_STATISTIC = "usage_statistic"
CONF_RATE_ENTITY = "current_rate_entity"
CONF_RATE_VALUE = "current_rate_value"
CONF_RATE_UNIT = "current_rate_unit"
CONF_THRESHOLD = "threshold_pct"
CONF_NO_CANCELLATION_FEE = "no_cancellation_fee"
CONF_PRIOR_SUPPLIERS = "prior_suppliers"
CONF_RATE_JUMP_PCT = "rate_jump_pct"
CONF_THERMS_PER_MCF = "therms_per_mcf"

DEFAULT_SERVICE_TYPE = "residential"
DEFAULT_THRESHOLD = 5.0
DEFAULT_RATE_JUMP_PCT = 10.0
PRICE_UNITS = ["mcf", "ccf", "therm"]

# How much history to read from the usage statistic.
USAGE_LOOKBACK_DAYS = 3 * 365

EVENT_RECOMMENDATION_CHANGED = f"{DOMAIN}_recommendation_changed"

ISSUE_CHEAPER_GAS = "cheaper_gas_supplier"
ISSUE_RATE_JUMP = "gas_rate_jump"

CURRENCY = "USD"
SHOP_URL = "https://www.pagasswitch.com/shop-for-rates/"
LEADERBOARD_SIZE = 15
