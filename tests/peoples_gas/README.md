# peoples_gas integration tests

Offline Home Assistant tests: the portal client is replaced by `FakePortal` in
`conftest.py`, so no credentials or network are needed.

```bash
python3.13 -m venv .venv-ha
.venv-ha/bin/pip install pytest-homeassistant-custom-component beautifulsoup4 pdfplumber
.venv-ha/bin/pytest tests/peoples_gas
```

Last run against Home Assistant 2026.2.3 (pytest-homeassistant-custom-component 0.13.316).
