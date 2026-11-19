# energy_shopper integration tests

Offline Home Assistant tests: PA Gas Switch is replaced by `FakeMarket` in `conftest.py`,
and usage comes from an external statistic imported into the test recorder, so no network
is needed. Uses the same venv as `tests/peoples_gas` (see its README):

```bash
.venv-ha/bin/pytest tests/energy_shopper
```

Last run against Home Assistant 2026.2.3 (pytest-homeassistant-custom-component 0.13.316).
