# duquesne_collector integration tests

Offline Home Assistant tests: the collector's API is faked by `FakeCollector` in
`conftest.py` behind `aioclient_mock`, so no collector, network or credentials are
needed. Uses the same venv as `tests/peoples_gas` (see its README):

```bash
.venv-ha/bin/pytest tests/duquesne_collector
```

Last run against Home Assistant 2026.2.3 (pytest-homeassistant-custom-component 0.13.316).
