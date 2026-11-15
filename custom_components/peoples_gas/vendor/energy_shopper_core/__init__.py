"""
Shared logic for comparing energy supplier offers against your own usage.

No Home Assistant imports, no environment variables, no fixed file paths: callers
(the standalone notifier, Home Assistant integrations) pass configuration in.

  models       Rate / Offer
  markets      per-state offer sources (markets.pa: PA Power Switch, PA Gas Switch)
  ranking      annual-cost ranking, best-by-category, Time-of-Use options, lock-in advice
  eligibility  new-customer-only offers vs. suppliers you have used
  usage        electric usage window and annual projection
  gas          gas unit conversion and seasonal forecast
  tou          Time-of-Use period classification and cost analysis
  alerts       market trend and own-rate jump messages
  report       report dataclasses and text/HTML rendering
  utilities    utility account clients (utilities.peoples_gas)
"""
