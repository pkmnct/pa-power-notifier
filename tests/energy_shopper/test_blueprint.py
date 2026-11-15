"""The shipped notify blueprint, run as a real automation on the integration's event."""

from __future__ import annotations

from pathlib import Path
import shutil

from custom_components.energy_shopper.const import EVENT_RECOMMENDATION_CHANGED
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.common import async_mock_service

BLUEPRINT = Path(__file__).parents[2] / "blueprints/automation/energy_shopper/notify_on_recommendation.yaml"

SWITCH = {
    "recommendation": "switch", "previous": "stay", "supplier": "Welcome Gas",
    "plan_type": "Fixed", "term": "12 Months", "price": 2.8, "unit": "mcf",
    "current_price": 3.57, "annual_savings": 55.6, "savings_pct": 21.6, "phone": "800-555-0199",
}


async def _automation(hass: HomeAssistant, include_stay: bool, fuels: list[str] | None = None) -> list:
    dest = Path(hass.config.path("blueprints/automation/energy_shopper"))
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy(BLUEPRINT, dest / BLUEPRINT.name)
    calls = async_mock_service(hass, "notify", "phone")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "use_blueprint": {
                    "path": f"energy_shopper/{BLUEPRINT.name}",
                    "input": {
                        "notify_action": "notify.phone",
                        "include_stay": include_stay,
                        **({"fuels": fuels} if fuels is not None else {}),
                    },
                }
            }
        },
    )
    await hass.async_block_till_done()
    return calls


async def test_switch_notifies(hass: HomeAssistant) -> None:
    calls = await _automation(hass, include_stay=False)
    hass.bus.async_fire(EVENT_RECOMMENDATION_CHANGED, SWITCH)
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0].data["title"] == "Cheaper gas: Welcome Gas"
    msg = " ".join(calls[0].data["message"].split())
    assert "is $2.8000/mcf vs your $3.5700/mcf, about $56/yr (22%) less. Call 800-555-0199." in msg

    hass.bus.async_fire(EVENT_RECOMMENDATION_CHANGED, {"recommendation": "stay", "previous": "switch"})
    await hass.async_block_till_done()
    assert len(calls) == 1  # stay is opt-in


async def test_stay_when_asked(hass: HomeAssistant) -> None:
    calls = await _automation(hass, include_stay=True)
    hass.bus.async_fire(EVENT_RECOMMENDATION_CHANGED, {"recommendation": "stay", "previous": "switch", "supplier": None})
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0].data["title"] == "Gas: stay on your current rate"


TOU_EVENT = {
    "fuel": "electric", "recommendation": "switch", "previous": "stay", "supplier": "Duquesne Light",
    "plan_type": "Time-of-Use Supply (Rider No. 8)", "time_of_use": True, "term": "Tariff / pilot",
    "price": 0.0981, "unit": "kWh", "current_price": 0.15, "annual_savings": 512.4, "savings_pct": 34.6, "phone": None,
}


async def test_electric_time_of_use(hass: HomeAssistant) -> None:
    calls = await _automation(hass, include_stay=False)
    hass.bus.async_fire(EVENT_RECOMMENDATION_CHANGED, TOU_EVENT)
    await hass.async_block_till_done()
    assert calls[0].data["title"] == "Electric: Time-of-Use would save"
    msg = " ".join(calls[0].data["message"].split())
    assert msg.startswith("Duquesne Light Time-of-Use Supply (Rider No. 8) is $0.0981/kWh vs your $0.1500/kWh, about $512/yr (35%) less.")


async def test_fuel_filter(hass: HomeAssistant) -> None:
    calls = await _automation(hass, include_stay=True, fuels=["gas"])
    hass.bus.async_fire(EVENT_RECOMMENDATION_CHANGED, TOU_EVENT)
    hass.bus.async_fire(EVENT_RECOMMENDATION_CHANGED, {**TOU_EVENT, "recommendation": "stay"})
    await hass.async_block_till_done()
    assert calls == []
