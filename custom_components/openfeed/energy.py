"""Add openfeed meter data to the Home Assistant Energy dashboard.

The meter statistics are external (openfeed:<meter_key>_consumption and
_export), hourly and a day behind. They can be a grid connection in the
Energy dashboard, but Home Assistant only prices external statistics from a
cost statistic, so the connection is added without a price.

The user chooses in the config flow or the options what to do:

* no_change  leave the Energy dashboard alone (default)
* add        add one grid connection per meter, keeping every other source
* replace    remove the existing grid connections and add the meter's

The choice is applied once, after the first refresh that has meter data, and
then reset to no_change, so a later manual edit is never undone.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .statistics import meter_label, statistic_id

_LOGGER = logging.getLogger(__name__)

ENERGY_NO_CHANGE = "no_change"
ENERGY_ADD = "add"
ENERGY_REPLACE = "replace"
ENERGY_CHOICES = [ENERGY_NO_CHANGE, ENERGY_ADD, ENERGY_REPLACE]


def grid_sources(energy_accounts: dict[str, Any]) -> list[dict[str, Any]]:
    """One Energy dashboard grid connection per meter with imported usage."""
    sources: list[dict[str, Any]] = []
    for account in energy_accounts.values():
        for meter in account.meters.values():
            usage = meter.usage
            series = set(usage.hourly) if usage else set(meter.daily_history)
            if "consumption" not in series:
                continue
            sources.append(
                {
                    "type": "grid",
                    "name": f"openfeed {meter_label(meter.meter).lower()}",
                    "stat_energy_from": statistic_id(meter.key, "consumption"),
                    "stat_energy_to": (
                        statistic_id(meter.key, "export") if "export" in series else None
                    ),
                    # External statistics cannot be priced from an entity or a
                    # number; only a cost statistic works, and there is none.
                    "stat_cost": None,
                    "entity_energy_price": None,
                    "number_energy_price": None,
                    "stat_compensation": None,
                    "entity_energy_price_export": None,
                    "number_energy_price_export": None,
                    "cost_adjustment_day": 0.0,
                }
            )
    return sources


def merge_sources(
    current: list[dict[str, Any]], ours: list[dict[str, Any]], mode: str
) -> list[dict[str, Any]] | None:
    """The new energy_sources list, or None when nothing would change."""
    if mode not in (ENERGY_ADD, ENERGY_REPLACE) or not ours:
        return None
    keep = [s for s in current if not (mode == ENERGY_REPLACE and s.get("type") == "grid")]
    present = {s.get("stat_energy_from") for s in keep if s.get("type") == "grid"}
    added = [s for s in ours if s["stat_energy_from"] not in present]
    new = keep + added
    return None if new == current else new


async def async_apply_energy_choice(
    hass: HomeAssistant, energy_accounts: dict[str, Any], mode: str
) -> bool:
    """Apply the user's Energy dashboard choice. True when it is settled.

    Returns False only when there is no meter data yet, so the choice is
    kept and tried again after the next setup.
    """
    if mode not in (ENERGY_ADD, ENERGY_REPLACE):
        return True
    ours = grid_sources(energy_accounts)
    if not ours:
        return False
    if "energy" not in hass.config.components:
        _LOGGER.warning(
            "The Energy integration is not loaded, so openfeed did not add its meter data "
            "to the Energy dashboard. Add it in Settings > Dashboards > Energy"
        )
        return True
    from homeassistant.components.energy.data import (  # noqa: PLC0415
        ENERGY_SOURCE_SCHEMA,
        async_get_manager,
    )

    manager = await async_get_manager(hass)
    prefs = manager.data or manager.default_preferences()
    current = [dict(s) for s in prefs.get("energy_sources", [])]
    new = merge_sources(current, ours, mode)
    if new is None:
        return True
    try:
        ENERGY_SOURCE_SCHEMA(new)
    except Exception as err:  # noqa: BLE001 - never save prefs the frontend would reject
        _LOGGER.warning("openfeed did not change the Energy dashboard: %s", err)
        return True
    await manager.async_update({"energy_sources": new})
    _LOGGER.info(
        "openfeed %s the Energy dashboard grid connections: %s",
        "replaced" if mode == ENERGY_REPLACE else "added to",
        ", ".join(s["stat_energy_from"] for s in ours),
    )
    return True
