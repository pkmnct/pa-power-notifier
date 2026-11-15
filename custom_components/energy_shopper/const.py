"""Constants for the Energy Shopper integration (Pennsylvania gas and electric)."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "energy_shopper"

# PA Power/Gas Switch publish offer changes during the day; twice a day is plenty and polite.
UPDATE_INTERVAL = timedelta(hours=12)

# Config entry data: which fuel and market to shop. Entries without a fuel are gas
# (the first version only did gas).
CONF_FUEL = "fuel"
FUEL_GAS = "gas"
FUEL_ELECTRIC = "electric"
CONF_SHOP_URL = "shop_url"  # electric: PA Power Switch results URL
CONF_RATE_TYPE = "rate_type"  # electric: rate class, from that URL
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
# Electric only
CONF_SUPPLIER_ENTITY = "current_supplier_entity"
CONF_SUPPLIER_NAME = "current_supplier"
CONF_PTC_ENTITY = "price_to_compare_entity"
CONF_PTC_VALUE = "price_to_compare_value"
CONF_RENEWABLE_ONLY = "renewable_only"
CONF_TOU_PEAK = "tou_peak_cents"
CONF_TOU_OFF_PEAK = "tou_off_peak_cents"
CONF_TOU_SUPER_OFF_PEAK = "tou_super_off_peak_cents"

DEFAULT_SERVICE_TYPE = "residential"
DEFAULT_THRESHOLD = 5.0
DEFAULT_RATE_JUMP_PCT = 10.0
PRICE_UNITS = ["mcf", "ccf", "therm"]

# How much history to read from the usage statistic.
USAGE_LOOKBACK_DAYS = 3 * 365  # gas, monthly
HOURLY_LOOKBACK_DAYS = 400  # electric, hourly (what Opower keeps)
# A day counts as hourly data (for Time-of-Use) with readings in at least this many hours.
TOU_MIN_HOURS_PER_DAY = 20

EVENT_RECOMMENDATION_CHANGED = f"{DOMAIN}_recommendation_changed"

ISSUE_CHEAPER = "cheaper_{fuel}_supplier"
ISSUE_RATE_JUMP = "{fuel}_rate_jump"
ISSUE_INPUT_GONE = "input_sensor_missing"

CURRENCY = "USD"
SHOP_URL = "https://www.pagasswitch.com/shop-for-rates/"
ELECTRIC_SHOP_URL = "https://www.papowerswitch.com/shop-for-rates/"
LEADERBOARD_SIZE = 15
