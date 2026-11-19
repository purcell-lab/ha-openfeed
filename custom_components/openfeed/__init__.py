"""The openfeed Consumer Data Right integration."""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CONF_GRANT_ID, CONF_TOKEN, DOMAIN
from .dashboard import async_register_dashboard, async_remove_dashboard, wants_dashboard
from .coordinator import OpenfeedConfigEntry, OpenfeedCoordinator
from .oauth import OpenfeedAuthError
from .session import OpenfeedSession, build_auth
from .views import async_register_views

_LOGGER = logging.getLogger(__name__)

REVOKE_TIMEOUT_SECONDS = 15

PLATFORMS: list[Platform] = [Platform.CALENDAR, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the redirect views so a reauth flow can complete."""
    async_register_views(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: OpenfeedConfigEntry) -> bool:
    """Migrate an older config entry."""
    from .const import CONF_METER_KEY_SCHEME, METER_KEY_LEGACY

    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        # Keep the nmi_<nmi> meter keys and names this entry's statistics,
        # unique IDs, entity IDs and Energy dashboard sources already use.
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_METER_KEY_SCHEME: METER_KEY_LEGACY},
            minor_version=2,
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OpenfeedConfigEntry) -> bool:
    """Set up openfeed from a config entry."""
    async_register_views(hass)
    if not entry.data.get(CONF_GRANT_ID):
        raise ConfigEntryAuthFailed("No openfeed disclosure grant; reconnect to share data")
    session = OpenfeedSession(hass, entry)
    coordinator = OpenfeedCoordinator(hass, entry, session)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await _async_apply_energy_dashboard(hass, entry, coordinator)
    options_snapshot = dict(entry.options)

    async def _async_entry_updated(hass: HomeAssistant, entry: OpenfeedConfigEntry) -> None:
        # Token refreshes rewrite entry data every hour; only an options
        # change should reload the entry.
        if dict(entry.options) != options_snapshot:
            await hass.config_entries.async_reload(entry.entry_id)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    if wants_dashboard(hass):
        async_register_dashboard(hass)
    return True


async def _async_apply_energy_dashboard(
    hass: HomeAssistant, entry: OpenfeedConfigEntry, coordinator: OpenfeedCoordinator
) -> None:
    """Apply a pending Energy dashboard choice once, then reset it.

    Runs before the options snapshot, so resetting the option does not
    trigger a reload.
    """
    from .const import CONF_ENERGY_DASHBOARD
    from .energy import ENERGY_NO_CHANGE, async_apply_energy_choice

    mode = entry.options.get(CONF_ENERGY_DASHBOARD, ENERGY_NO_CHANGE)
    if mode == ENERGY_NO_CHANGE:
        return
    if await async_apply_energy_choice(hass, coordinator.data.energy, mode):
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_ENERGY_DASHBOARD: ENERGY_NO_CHANGE}
        )


async def async_unload_entry(hass: HomeAssistant, entry: OpenfeedConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    others = [
        e
        for e in hass.config_entries.async_loaded_entries(DOMAIN)
        if e.entry_id != entry.entry_id
    ]
    if not others or not wants_dashboard(hass):
        async_remove_dashboard(hass)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: OpenfeedConfigEntry) -> None:
    """Revoke the refresh token at openfeed when the integration is deleted.

    Best effort: a failure is logged and never blocks removal. The disclosure
    grant itself stays listed in the openfeed dashboard until the user
    revokes it there.
    """
    refresh_token = (entry.data.get(CONF_TOKEN) or {}).get("refresh_token")
    if not refresh_token:
        return
    try:
        async with asyncio.timeout(REVOKE_TIMEOUT_SECONDS):
            revoked = await build_auth(hass, dict(entry.data)).revoke(refresh_token)
    except (OpenfeedAuthError, aiohttp.ClientError, TimeoutError, ValueError) as err:
        _LOGGER.warning(
            "Could not revoke the openfeed refresh token (%s). Revoke the grant in the "
            "openfeed dashboard to stop sharing",
            err,
        )
        return
    if revoked:
        _LOGGER.info(
            "Revoked the openfeed refresh token. Revoke the disclosure grant in the "
            "openfeed dashboard to end sharing"
        )
