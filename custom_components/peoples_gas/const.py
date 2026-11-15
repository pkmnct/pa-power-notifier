"""Constants for the Peoples Gas integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "peoples_gas"

MANUFACTURER = "Peoples Natural Gas"
PORTAL_URL = "https://peopleseaccount.com/Portal/"

# Peoples serves western Pennsylvania; cycle dates from the portal are local dates.
UTILITY_TIME_ZONE = "America/New_York"

# The portal updates once per billing cycle; twice a day is plenty and stays polite.
UPDATE_INTERVAL = timedelta(hours=12)

CONF_SAVE_BILL_PDFS = "save_bill_pdfs"
DEFAULT_SAVE_BILL_PDFS = False

# Saved under the HA config directory: <config>/peoples_gas/<account>/<date>_<invoice>.pdf
BILL_PDF_DIR = "peoples_gas"

CURRENCY = "USD"
