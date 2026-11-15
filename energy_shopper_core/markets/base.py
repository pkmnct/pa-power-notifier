"""What every state market site has to provide to the ranking code."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, Tuple

from ..models import Rate


@dataclass
class OfferListing:
    offers: List[Rate]
    raw: str  # the site's response as fetched (CSV for Pennsylvania), for archiving


class MarketProvider(Protocol):
    """Supplier offers for one distributor (utility) and service type."""

    utility_type: str  # 'electric' | 'gas'

    def fetch_offers(self) -> OfferListing:
        """Current offers. Raises on a failed request."""
        ...

    def price_to_compare(self) -> Optional[Tuple[float, str]]:
        """Default-service supply price as (usd_per_unit, unit), or None if the site has none."""
        ...
