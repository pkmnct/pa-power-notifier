"""Constants for the Duquesne Light (collector) integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "duquesne_collector"

# HA Core runs on the host network on HA OS, so this reaches the add-on's published port
# (and a Compose service on the same machine).
DEFAULT_URL = "http://localhost:8765"

CONF_STATISTIC_KEY = "statistic_key"  # fixed at setup so a URL change keeps history

# The collector fetches once a day; checking hourly picks that up soon after.
UPDATE_INTERVAL = timedelta(hours=1)
REQUEST_TIMEOUT = 60  # first run pulls every archived hour

# Opower prices a period a day or two after the kWh read. Hours still unpriced after
# this long are given up on, so the cost statistic can move past them.
COST_GRACE = timedelta(days=7)

# The collector runs daily; past this without a success something is wrong.
STALE_AFTER = timedelta(hours=36)
UNREACHABLE_AFTER = timedelta(hours=24)

ISSUE_STALE = "collector_stale"
ISSUE_AUTH = "collector_auth"
ISSUE_UNREACHABLE = "collector_unreachable"

MANUFACTURER = "Duquesne Light"
CURRENCY = "USD"
DEFAULT_SERVICE = "Duquesne Light default service"
