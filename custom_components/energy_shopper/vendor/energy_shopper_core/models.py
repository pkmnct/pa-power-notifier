"""Offer model shared by market providers and the ranking code."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class Rate:
    """
    One supplier offer (or a utility's default service) as listed by a state market site.

    Fields keep the site's raw text; the get_*/has_*/is_* helpers parse them.
    """
    price: str
    supplier: str
    type: str
    cancellation_fee: str
    deposit_required: str
    discounts_incentives: str
    introductory_price: str
    monthly_fee: str
    more_info: str
    service_type: str
    term_end_date: str
    term_length: str
    utility_type: str  # 'electric' or 'gas'
    new_customers_offer: str = ''  # CSV "New Customers Offer": Yes = new customers only
    # Electric-only CSV columns (PA Gas Switch has no renewable data)
    renewable: str = ''  # "Renewable Energy": Yes / No
    pa_wind: str = ''  # "PA wind": Yes / No
    phone: str = ''  # "Contact Phone Number"

    def get_numeric_price(self) -> Optional[float]:
        try:
            price_str = self.price.replace('$', '').replace(',', '').strip()
            parts = price_str.split()
            if parts:
                return float(parts[0])
        except (ValueError, AttributeError):
            pass
        return None

    def get_numeric_monthly_fee(self) -> float:
        """Parse Monthly Fee column to dollars per month (0 if none / unknown)."""
        s = (self.monthly_fee or '').strip()
        if not s or s.lower() in ('none', 'n/a', '$0', '0', 'no'):
            return 0.0
        try:
            cleaned = s.replace('$', '').replace(',', '').strip()
            parts = cleaned.split()
            if parts:
                fee = float(parts[0])
                # e.g. "$1 per day"
                return fee * 30.4 if 'day' in cleaned.lower() else fee
        except (ValueError, AttributeError):
            pass
        return 0.0

    def has_cancellation_fee(self) -> bool:
        val = self.cancellation_fee.strip().lower()
        return val not in ('', 'none', 'no', '$0', '0', 'n/a')

    def renewable_tier(self) -> str:
        """'100%', 'partial' or 'none' from the electric CSV; '' when not reported (gas)."""
        flag = (self.renewable or '').strip().lower()
        if flag == 'yes':
            # "100%" counts only where it describes this plan, not an upsell like
            # "Interested in upgrading to 100% green energy for an additional 3 cents?"
            for sentence in re.split(r'(?<=[.!?])\s+', self.more_info or ''):
                if re.search(r'100\s*%', sentence) and not re.search(r'upgrad|additional', sentence, re.I):
                    return '100%'
            return 'partial'
        if flag == 'no':
            return 'partial' if (self.pa_wind or '').strip().lower() == 'yes' else 'none'
        return ''

    def is_renewable(self) -> bool:
        tier = self.renewable_tier()
        if tier:
            return tier != 'none'
        keywords = ('green', 'renewable', 'solar', 'wind', 'clean')
        searchable = (self.type + self.supplier + self.discounts_incentives).lower()
        return any(k in searchable for k in keywords)


Offer = Rate
