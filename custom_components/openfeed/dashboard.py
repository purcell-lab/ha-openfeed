"""A read-only sidebar dashboard generated from the integration's own data.

The dashboard is not stored in .storage/lovelace. It is built on every load
from the config entries, the entity registry and the statistic keys, so it
always matches the installed version and the accounts currently shared.
Turn it off in the integration options.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import CONF_SIDEBAR_DASHBOARD, DASHBOARD_URL_PATH, DOMAIN
from .statistics import account_key, statistic_id

_LOGGER = logging.getLogger(__name__)

TITLE = "Energy data"
# The CDR shares up to 2 years of usage; the long-range charts show all of it.
TWO_YEARS_DAYS = 730
ICON = "mdi:transmission-tower"


def _entity(registry: er.EntityRegistry, platform: str, unique_id: str) -> str | None:
    return registry.async_get_entity_id(platform, DOMAIN, unique_id)


def _rows(registry: er.EntityRegistry, base: str, items: list[tuple[str, str]]) -> list[dict]:
    """Entities rows for the unique_id suffixes that exist, with fixed names."""
    rows = []
    for suffix, name in items:
        if entity_id := _entity(registry, "sensor", f"{base}_{suffix}"):
            rows.append({"entity": entity_id, "name": name})
    return rows


def _card(title: str, rows: list[dict]) -> list[dict]:
    return [{"type": "entities", "title": title, "entities": rows}] if rows else []


def _account_cards(hass: HomeAssistant, entry: Any, account_id: str, account: Any) -> list[dict]:
    registry = er.async_get(hass)
    base = f"{entry.unique_id}_{account_id}"
    acct = account_key(account_id)
    cards: list[dict] = []
    cards += _card(
        "Bills and payments",
        _rows(
            registry,
            base,
            [
                ("balance", "Account balance"),
                ("latest_invoice_amount", "Latest invoice"),
                ("latest_invoice_due", "Due"),
                ("latest_invoice_status", "Status"),
                ("last_payment", "Last payment"),
                ("last_refund", "Last refund"),
            ],
        ),
    )
    cards += _card(
        "Plan tariff",
        _rows(
            registry,
            base,
            [
                ("plan", "Plan"),
                ("tariff_import_rate", "Import rate (inc GST)"),
                ("tariff_period", "Tariff period"),
                ("tariff_feed_in_rate", "Feed-in rate"),
                ("tariff_daily_supply_charge", "Daily supply charge (inc GST)"),
                ("tariff_demand_charge", "Demand charge (inc GST)"),
                ("tariff_next_change", "Next change"),
            ],
        ),
    )
    cards += _card(
        "Charges and credits",
        _rows(
            registry,
            base,
            [
                ("month_to_date_charges", "Charges this month"),
                ("month_to_date_credits", "Credits this month"),
                ("last_statement_charges", "Last statement charges"),
                ("last_statement_credits", "Last statement credits"),
            ],
        ),
    )
    billing = account.billing
    series = set(billing.hourly) if billing else set()
    money = [
        (s, name)
        for s, name in (
            ("charges", "Charges"),
            ("credits", "Credits"),
            ("payments", "Payments"),
            ("refunds", "Refunds"),
        )
        if s in series
    ]
    if money:
        cards.append(
            {
                "type": "statistics-graph",
                "title": "Monthly charges, credits and payments",
                "period": "month",
                "days_to_show": 365,
                "chart_type": "bar",
                "stat_types": ["change"],
                "hide_legend": False,
                "entities": [
                    {"entity": statistic_id(acct, s), "name": name} for s, name in money
                ],
            }
        )
    if calendar := _entity(registry, "calendar", f"{base}_bills"):
        cards.append(
            {"type": "calendar", "title": "Bills", "initial_view": "listWeek", "entities": [calendar]}
        )
    return cards


def _meter_cards(hass: HomeAssistant, entry: Any, meter: Any) -> list[dict]:
    registry = er.async_get(hass)
    base = f"{entry.unique_id}_{meter.key}"
    key = meter.key
    cards: list[dict] = []
    cards += _card(
        "Grid energy",
        _rows(
            registry,
            base,
            [
                ("month_to_date_import", "Import this month"),
                ("month_to_date_export", "Export this month"),
                ("last_30_days_import", "Import last 30 days"),
                ("last_30_days_export", "Export last 30 days"),
                ("latest_day_consumption", "Import latest day"),
                ("latest_day_export", "Export latest day"),
                ("latest_day_peak_import", "Peak import latest day"),
                ("latest_day_peak_export", "Peak export latest day"),
            ],
        ),
    )
    usage = meter.usage
    energy_series = set(usage.hourly) if usage else set()
    power_series = set(usage.hourly_power) if usage else set()
    if energy_series:
        cards.append(
            {
                "type": "statistics-graph",
                "title": "Daily grid energy (meter reads)",
                "period": "day",
                "days_to_show": 30,
                "chart_type": "bar",
                "stat_types": ["change"],
                "entities": [
                    {"entity": statistic_id(key, s), "name": name}
                    for s, name in (("consumption", "Import"), ("export", "Export"))
                    if s in energy_series
                ],
            }
        )
    if energy_series:
        cards.append(
            {
                "type": "statistics-graph",
                "title": "Monthly grid energy, 2 years (meter reads)",
                "period": "month",
                "days_to_show": TWO_YEARS_DAYS,
                "chart_type": "bar",
                "stat_types": ["change"],
                "entities": [
                    {"entity": statistic_id(key, s), "name": name}
                    for s, name in (("consumption", "Import"), ("export", "Export"))
                    if s in energy_series
                ],
            }
        )
    if power_series:
        power = [
            (s, name)
            for s, name in (("consumption", "import"), ("export", "export"))
            if s in power_series
        ]
        cards.append(
            {
                "type": "statistics-graph",
                "title": "Grid power, hourly mean and peak (last 7 days)",
                "period": "hour",
                "days_to_show": 7,
                "chart_type": "line",
                "stat_types": ["mean", "max"],
                "entities": [
                    {"entity": statistic_id(key, f"{s}_power"), "name": f"Grid {name}"}
                    for s, name in power
                ],
            }
        )
        cards.append(
            {
                "type": "statistics-graph",
                "title": "Daily peak grid power (5-minute reads)",
                "period": "day",
                "days_to_show": 30,
                "chart_type": "line",
                "stat_types": ["max"],
                "entities": [
                    {"entity": statistic_id(key, f"{s}_power"), "name": f"Peak {name}"}
                    for s, name in power
                ],
            }
        )
        cards.append(
            {
                "type": "statistics-graph",
                "title": "Weekly grid power, mean and 5-minute peak (2 years)",
                "period": "week",
                "days_to_show": TWO_YEARS_DAYS,
                "chart_type": "line",
                "stat_types": ["mean", "max"],
                "entities": [
                    {"entity": statistic_id(key, f"{s}_power"), "name": f"Grid {name}"}
                    for s, name in power
                ],
            }
        )
    cards += _card(
        "Solar and battery register",
        _rows(
            registry,
            base,
            [
                ("der_solar_capacity", "Solar capacity"),
                ("der_approved_capacity", "Approved inverter capacity"),
                ("der_export_limit", "Export limit"),
                ("der_register_issues", "Register issues"),
                ("usage_history_days", "Days of usage history"),
                ("usage_fetched", "Usage last fetched"),
            ],
        ),
    )
    cards += _card(
        "Meter",
        _rows(
            registry,
            base,
            [
                ("meter_status", "Status"),
                ("meter_read_type", "Read type"),
                ("meter_jurisdiction", "Jurisdiction"),
                ("meter_classification", "Classification"),
                ("meter_generator", "Generator"),
            ],
        ),
    )
    return cards


def build_dashboard(hass: HomeAssistant) -> dict[str, Any]:
    """The dashboard config for every loaded openfeed entry."""
    views: list[dict] = []
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        coordinator = getattr(entry, "runtime_data", None)
        data = getattr(coordinator, "data", None)
        if data is None:
            continue
        for account_id, account in data.energy.items():
            provider = account.account.get("providerName") or "Energy"
            cards = _account_cards(hass, entry, account_id, account)
            for meter in account.meters.values():
                cards = _meter_cards(hass, entry, meter) + cards
            suffix = f"-{len(views) + 1}" if views else ""
            views.append(
                {
                    "title": provider,
                    "path": f"account{suffix}",
                    "icon": ICON,
                    "cards": cards,
                }
            )
    if not views:
        views.append(
            {
                "title": TITLE,
                "path": "account",
                "cards": [
                    {
                        "type": "markdown",
                        "content": "No openfeed data yet. Check the integration in "
                        "**Settings > Devices & services**.",
                    }
                ],
            }
        )
    return {"title": TITLE, "views": views}


def _lovelace_config_class() -> type | None:
    """A LovelaceConfig subclass, or None when the frontend internals moved."""
    try:
        from homeassistant.components.lovelace.dashboard import LovelaceConfig
        from homeassistant.helpers.json import json_bytes, json_fragment
    except ImportError:  # pragma: no cover - depends on the Core version
        return None

    class OpenfeedDashboard(LovelaceConfig):
        """Serves the generated config as a read-only (YAML mode) dashboard."""

        @property
        def mode(self) -> str:
            return "yaml"

        async def async_get_info(self) -> dict[str, Any]:
            config = build_dashboard(self.hass)
            return {"mode": self.mode, "views": len(config["views"])}

        async def async_load(self, force: bool) -> dict[str, Any]:
            return build_dashboard(self.hass)

        async def async_json(self, force: bool) -> Any:
            return json_fragment(json_bytes(build_dashboard(self.hass)))

    return OpenfeedDashboard


def wants_dashboard(hass: HomeAssistant) -> bool:
    """True when any loaded or loading entry has the dashboard switched on."""
    return any(
        entry.options.get(CONF_SIDEBAR_DASHBOARD, True)
        for entry in hass.config_entries.async_entries(DOMAIN)
        if not entry.disabled_by
    )


@callback
def async_register_dashboard(hass: HomeAssistant) -> bool:
    """Add the sidebar dashboard. Returns False when it could not be added."""
    try:
        from homeassistant.components import frontend
        from homeassistant.components.lovelace.const import LOVELACE_DATA
    except ImportError:  # pragma: no cover
        return False
    lovelace = hass.data.get(LOVELACE_DATA)
    config_class = _lovelace_config_class()
    if lovelace is None or config_class is None:
        _LOGGER.warning("Lovelace is not loaded; the openfeed dashboard was not added")
        return False
    existing = lovelace.dashboards.get(DASHBOARD_URL_PATH)
    if existing is not None and type(existing).__name__ != "OpenfeedDashboard":
        _LOGGER.warning(
            "A dashboard already uses /%s; the openfeed dashboard was not added",
            DASHBOARD_URL_PATH,
        )
        return False
    lovelace.dashboards[DASHBOARD_URL_PATH] = config_class(
        hass, DASHBOARD_URL_PATH, {"title": TITLE, "icon": ICON, "mode": "yaml"}
    )
    frontend.async_register_built_in_panel(
        hass,
        "lovelace",
        frontend_url_path=DASHBOARD_URL_PATH,
        sidebar_title=TITLE,
        sidebar_icon=ICON,
        require_admin=False,
        config={"mode": "yaml"},
        update=existing is not None,
    )
    return True


@callback
def async_remove_dashboard(hass: HomeAssistant) -> None:
    """Remove the sidebar dashboard if openfeed added it."""
    try:
        from homeassistant.components import frontend
        from homeassistant.components.lovelace.const import LOVELACE_DATA
    except ImportError:  # pragma: no cover
        return
    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None:
        return
    existing = lovelace.dashboards.get(DASHBOARD_URL_PATH)
    if existing is None or type(existing).__name__ != "OpenfeedDashboard":
        return
    lovelace.dashboards.pop(DASHBOARD_URL_PATH, None)
    frontend.async_remove_panel(hass, DASHBOARD_URL_PATH, warn_if_unknown=False)
