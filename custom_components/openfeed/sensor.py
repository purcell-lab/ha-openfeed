"""Sensors for openfeed energy and banking accounts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfApparentPower,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DASHBOARD_URL, DOMAIN, ENERGY_CONFIG_URL
from .coordinator import (
    BankingAccountData,
    EnergyAccountData,
    MeterData,
    OpenfeedConfigEntry,
    OpenfeedCoordinator,
    latest_day_peak,
    latest_day_total,
    period_total,
)
from .statistics import account_key, meter_label
from . import tariff
from .usage import AEST, CONSUMPTION, CONTROLLED_LOAD, EXPORT


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


# Energy account sensors --------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class AccountSensorDescription(SensorEntityDescription):
    value_fn: Callable[[EnergyAccountData], Any]
    attrs_fn: Callable[[EnergyAccountData], dict[str, Any]] = lambda _a: {}
    available_fn: Callable[[EnergyAccountData], bool] = lambda _a: True
    # Only create the entity when this returns True at setup.
    exists_fn: Callable[[EnergyAccountData], bool] = lambda _a: True
    # Unit as "<currency>/<suffix>", for rates such as AUD/kWh.
    unit_suffix: str | None = None
    # Re-evaluate every minute, for values that follow the clock.
    clock: bool = False


def _plan(a: EnergyAccountData) -> dict[str, Any]:
    return (a.plan or {}).get("planOverview") or {}


def _billing_attrs(a: EnergyAccountData) -> dict[str, Any]:
    b = a.billing
    key = account_key(a.account["accountId"])
    return {
        "charges_by_type": b.charges_by_type if b else None,
        "credits_by_type": b.credits_by_type if b else None,
        "transactions": b.transactions if b else None,
        "oldest_transaction": b.oldest_transaction.isoformat() if b and b.oldest_transaction else None,
        "statistic_prefix": f"{DOMAIN}:{key}_",
        "freshness": "batch, refreshed by openfeed every 6 hours",
    }


def _invoice(a: EnergyAccountData, key: str) -> Any:
    return (a.latest_invoice or {}).get(key)


def _payment_attrs(record: dict[str, Any] | None) -> dict[str, Any]:
    if not record:
        return {}
    return {"date": record.get("date"), "method": record.get("method")}


def _statement_attrs(a: EnergyAccountData) -> dict[str, Any]:
    b = a.billing
    if b is None or b.last_statement_date is None:
        return {}
    charges = b.last_statement_charges or 0.0
    credits = b.last_statement_credits or 0.0
    return {
        "statement_date": b.last_statement_date.isoformat(),
        "charges": charges,
        "credits": credits,
        "net": round(charges - credits, 2),
        "statistics": [
            f"{DOMAIN}:{account_key(a.account['accountId'])}_{s}"
            for s in ("charges", "credits", "payments", "refunds", "invoiced")
        ],
    }


def _detail(a: EnergyAccountData) -> dict[str, Any] | None:
    return (a.plan or {}).get("planDetail") or None


def _tariff(a: EnergyAccountData) -> tariff.TariffNow | None:
    return tariff.evaluate(_detail(a), dt_util.now(), dt_util.get_default_time_zone())


def _tariff_value(a: EnergyAccountData, field: str, gst: bool) -> float | None:
    now = _tariff(a)
    value = getattr(now, field) if now else None
    return tariff.inc_gst(value) if gst else value


def _import_attrs(a: EnergyAccountData) -> dict[str, Any]:
    now = _tariff(a)
    if now is None:
        return {}
    return {
        "rate_ex_gst": now.import_ex_gst,
        "gst_rate": tariff.GST_RATE,
        "time_of_use_period": now.import_type,
        "rate_name": now.import_name,
        "tariff_periods": now.periods,
        "blocks": now.import_blocks,
        "components": now.components,
        "source": "CDR plan detail (unit prices exclude GST; state includes GST)",
    }


def _supply_attrs(a: EnergyAccountData) -> dict[str, Any]:
    now = _tariff(a)
    return {"charge_ex_gst": now.supply_ex_gst, "gst_rate": tariff.GST_RATE} if now else {}


def _demand_attrs(a: EnergyAccountData) -> dict[str, Any]:
    now = _tariff(a)
    if now is None:
        return {}
    return {
        "charge_ex_gst": now.demand_ex_gst,
        "measure_unit": now.demand_unit,
        "charge_name": now.demand_name,
        **(now.demand_detail or {}),
    }


def _feed_in_attrs(a: EnergyAccountData) -> dict[str, Any]:
    detail = _detail(a) or {}
    now = _tariff(a)
    return {
        "tariff_name": now.feed_in_name if now else None,
        "tariffs": [
            {
                "name": f.get("displayName"),
                "scheme": f.get("scheme"),
                "payer": f.get("payerType"),
                "type": f.get("tariffUType"),
            }
            for f in detail.get("solarFeedInTariff") or []
        ],
    }


def _tou_value(a: EnergyAccountData) -> str | None:
    now = _tariff(a)
    value = now.import_type.lower() if now and now.import_type else None
    return value if value in TOU_OPTIONS else None


def _next_change(a: EnergyAccountData) -> datetime | None:
    return tariff.next_change(_detail(a), dt_util.now(), dt_util.get_default_time_zone())


TOU_OPTIONS = [t.lower() for t in (tariff.SINGLE_RATE, *tariff.TOU_TYPES)]


TARIFF_SENSORS: tuple[AccountSensorDescription, ...] = (
    AccountSensorDescription(
        key="tariff_import_rate",
        translation_key="tariff_import_rate",
        unit_suffix="kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        clock=True,
        value_fn=lambda a: _tariff_value(a, "import_ex_gst", True),
        attrs_fn=_import_attrs,
        exists_fn=lambda a: tariff.has_import(_detail(a)),
    ),
    AccountSensorDescription(
        key="tariff_period",
        translation_key="tariff_period",
        device_class=SensorDeviceClass.ENUM,
        options=TOU_OPTIONS,
        clock=True,
        value_fn=_tou_value,
        exists_fn=lambda a: tariff.has_import(_detail(a)),
    ),
    AccountSensorDescription(
        key="tariff_feed_in_rate",
        translation_key="tariff_feed_in_rate",
        unit_suffix="kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        clock=True,
        value_fn=lambda a: _tariff_value(a, "feed_in", False),
        attrs_fn=_feed_in_attrs,
        exists_fn=lambda a: tariff.has_feed_in(_detail(a)),
    ),
    AccountSensorDescription(
        key="tariff_daily_supply_charge",
        translation_key="tariff_daily_supply_charge",
        unit_suffix="d",
        suggested_display_precision=4,
        clock=True,
        value_fn=lambda a: _tariff_value(a, "supply_ex_gst", True),
        attrs_fn=_supply_attrs,
        exists_fn=lambda a: tariff.has_supply(_detail(a)),
    ),
    AccountSensorDescription(
        key="tariff_demand_charge",
        translation_key="tariff_demand_charge",
        suggested_display_precision=4,
        clock=True,
        value_fn=lambda a: _tariff_value(a, "demand_ex_gst", True),
        attrs_fn=_demand_attrs,
        exists_fn=lambda a: tariff.has_demand(_detail(a)),
    ),
    AccountSensorDescription(
        key="tariff_next_change",
        translation_key="tariff_next_change",
        device_class=SensorDeviceClass.TIMESTAMP,
        clock=True,
        value_fn=_next_change,
        exists_fn=lambda a: tariff.has_import(_detail(a)) or tariff.has_feed_in(_detail(a)),
    ),
)


ACCOUNT_SENSORS: tuple[AccountSensorDescription, ...] = (
    AccountSensorDescription(
        key="balance",
        translation_key="energy_balance",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda a: _num((a.balance or {}).get("balance")),
        attrs_fn=lambda a: {"freshness": "live, cached up to 15 minutes", "status": a.balance_status},
        available_fn=lambda a: a.balance is not None,
    ),
    AccountSensorDescription(
        key="latest_invoice_amount",
        translation_key="latest_invoice_amount",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda a: _num(_invoice(a, "invoiceAmount")),
        attrs_fn=lambda a: {
            "issue_date": _invoice(a, "issueDate"),
            "due_date": _invoice(a, "dueDate"),
            "payment_status": _invoice(a, "paymentStatus"),
            "gst_amount": _invoice(a, "gstAmount"),
            "balance_at_issue": _invoice(a, "balanceAtIssue"),
            "invoices_shared": a.invoice_count,
            "oldest_invoice": (
                a.billing.oldest_invoice.isoformat()
                if a.billing and a.billing.oldest_invoice
                else None
            ),
        },
        available_fn=lambda a: a.latest_invoice is not None,
    ),
    AccountSensorDescription(
        key="latest_invoice_due",
        translation_key="latest_invoice_due",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda a: _date(_invoice(a, "dueDate")),
        available_fn=lambda a: a.latest_invoice is not None,
    ),
    AccountSensorDescription(
        key="latest_invoice_status",
        translation_key="latest_invoice_status",
        value_fn=lambda a: _invoice(a, "paymentStatus"),
        available_fn=lambda a: a.latest_invoice is not None,
    ),
    AccountSensorDescription(
        key="plan",
        translation_key="plan",
        value_fn=lambda a: (_plan(a).get("displayName") or None),
        attrs_fn=lambda a: {
            "start_date": _plan(a).get("startDate"),
            "end_date": _plan(a).get("endDate"),
            "fuel_type": (_detail(a) or {}).get("fuelType"),
            "fees": (_detail(a) or {}).get("fees"),
            "discounts": (_detail(a) or {}).get("discounts"),
            "incentives": (_detail(a) or {}).get("incentives"),
            "plan_detail": (a.plan or {}).get("planDetail"),
        },
        available_fn=lambda a: a.plan is not None,
        exists_fn=lambda a: a.plan is not None,
    ),
    AccountSensorDescription(
        key="month_to_date_charges",
        translation_key="month_to_date_charges",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda a: a.billing.month_to_date_charges if a.billing else None,
        attrs_fn=_billing_attrs,
        available_fn=lambda a: a.billing is not None and a.billing.transactions > 0,
        exists_fn=lambda a: a.billing is not None and a.billing.transactions > 0,
    ),
    AccountSensorDescription(
        key="last_statement_charges",
        translation_key="last_statement_charges",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda a: a.billing.last_statement_charges if a.billing else None,
        attrs_fn=lambda a: _statement_attrs(a),
        available_fn=lambda a: bool(a.billing and a.billing.last_statement_date),
        exists_fn=lambda a: a.billing is not None and a.billing.transactions > 0,
    ),
    AccountSensorDescription(
        key="last_statement_credits",
        translation_key="last_statement_credits",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda a: a.billing.last_statement_credits if a.billing else None,
        attrs_fn=lambda a: _statement_attrs(a),
        available_fn=lambda a: bool(a.billing and a.billing.last_statement_date),
        exists_fn=lambda a: a.billing is not None and a.billing.transactions > 0,
    ),
    AccountSensorDescription(
        key="last_payment",
        translation_key="last_payment",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda a: (a.billing.last_payment or {}).get("amount") if a.billing else None,
        attrs_fn=lambda a: _payment_attrs(a.billing.last_payment if a.billing else None),
        available_fn=lambda a: bool(a.billing and a.billing.last_payment),
        exists_fn=lambda a: a.billing is not None and a.billing.transactions > 0,
    ),
    AccountSensorDescription(
        key="last_refund",
        translation_key="last_refund",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda a: (a.billing.last_refund or {}).get("amount") if a.billing else None,
        attrs_fn=lambda a: _payment_attrs(a.billing.last_refund if a.billing else None),
        available_fn=lambda a: bool(a.billing and a.billing.last_refund),
        # Only for accounts that have had a refund.
        exists_fn=lambda a: bool(a.billing and a.billing.last_refund),
    ),
    AccountSensorDescription(
        key="month_to_date_credits",
        translation_key="month_to_date_credits",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda a: a.billing.month_to_date_credits if a.billing else None,
        attrs_fn=_billing_attrs,
        available_fn=lambda a: a.billing is not None and a.billing.transactions > 0,
        exists_fn=lambda a: a.billing is not None and a.billing.transactions > 0,
    ),
)


# Meter sensors -----------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class MeterSensorDescription(SensorEntityDescription):
    value_fn: Callable[[MeterData], Any]
    attrs_fn: Callable[[MeterData], dict[str, Any]] = lambda _m: {}
    exists_fn: Callable[[MeterData], bool] = lambda _m: True


def _peak_sensor(series: str, key: str) -> MeterSensorDescription:
    def value(m: MeterData) -> float | None:
        peak = latest_day_peak(m.usage, series)
        return peak[1] if peak else None

    def attrs(m: MeterData) -> dict[str, Any]:
        peak = latest_day_peak(m.usage, series)
        return {
            "date": peak[0].isoformat() if peak else None,
            "interval_start": peak[2].isoformat() if peak else None,
            "interval_minutes": m.usage.interval_minutes if m.usage else None,
            "long_term_statistic": f"{DOMAIN}:{m.key}_{series}_power",
        }

    return MeterSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
        value_fn=value,
        attrs_fn=attrs,
        exists_fn=lambda m: bool(m.usage and m.usage.daily_peak.get(series)),
    )


def _period_sensor(series: str, key: str, period: str) -> MeterSensorDescription:
    """kWh over the month to date or the last 30 full days (today excluded)."""

    def bounds() -> tuple[date, date]:
        today = dt_util.now(AEST).date()
        if period == "month":
            return today.replace(day=1), today
        return today - timedelta(days=30), today

    def value(m: MeterData) -> float | None:
        start, end = bounds()
        total = period_total(m.daily_history, series, start, end)
        return total[0] if total else None

    def attrs(m: MeterData) -> dict[str, Any]:
        start, end = bounds()
        total = period_total(m.daily_history, series, start, end)
        return {
            "start": start.isoformat(),
            "end": (end - timedelta(days=1)).isoformat(),
            "days_with_data": total[1] if total else 0,
            "long_term_statistic": f"{DOMAIN}:{m.key}_{series}",
        }

    return MeterSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=value,
        attrs_fn=attrs,
        exists_fn=lambda m: bool(m.daily_history.get(series)),
    )


def _history_attrs(m: MeterData) -> dict[str, Any]:
    days = sorted({d for v in m.daily_history.values() for d in v})
    return {
        "oldest": days[0].isoformat() if days else None,
        "latest": days[-1].isoformat() if days else None,
        "statistics": [
            f"{DOMAIN}:{m.key}_{s}"
            for s in (CONSUMPTION, EXPORT, f"{CONSUMPTION}_power", f"{EXPORT}_power")
        ],
    }


def _der_summary_attrs(m: MeterData) -> dict[str, Any]:
    d = m.der_summary
    if d is None:
        return {}
    return {
        "active_inverter_kw": d.active_inverter_kw,
        "active_inverters": d.active_inverters,
        "storage_kwh": d.active_storage_kwh,
        "approved_capacity_kva": d.approved_capacity,
        "inverters": d.inverters,
        "source": "AEMO DER Register via the retailer",
    }


def _day_sensor(series: str) -> MeterSensorDescription:
    def value(m: MeterData) -> float | None:
        total = latest_day_total(m.usage, series)
        return total[1] if total else None

    def attrs(m: MeterData) -> dict[str, Any]:
        total = latest_day_total(m.usage, series)
        return {
            "date": total[0].isoformat() if total else None,
            "interval_minutes": m.usage.interval_minutes if m.usage else None,
            "freshness": "batch, refreshed by openfeed every 6 hours",
        }

    return MeterSensorDescription(
        key=f"latest_day_{series}",
        translation_key=f"latest_day_{series}",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=value,
        attrs_fn=attrs,
    )


def _der(m: MeterData, key: str) -> Any:
    return (m.der or {}).get(key)


def _meter_field(m: MeterData, key: str) -> Any:
    value = m.meter.get(key)
    return None if value in (None, "") else value


def _meter_detail_attrs(m: MeterData) -> dict[str, Any]:
    return {
        "detail_source": m.detail_source,
        "detail_fetched": m.detail_fetched_at.isoformat() if m.detail_fetched_at else None,
        "status": m.meter.get("status"),
        "read_type": m.meter.get("detectedReadType"),
        "jurisdiction": m.meter.get("jurisdictionCode"),
        "service_point_classification": m.meter.get("servicePointClassification"),
        "is_generator": m.meter.get("isGenerator"),
    }


def _generator(m: MeterData) -> str | None:
    value = m.meter.get("isGenerator")
    return None if value is None else ("yes" if value else "no")


METER_DETAIL_SENSORS: tuple[MeterSensorDescription, ...] = (
    MeterSensorDescription(
        key="meter_status",
        translation_key="meter_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: (str(_meter_field(m, "status") or "").lower() or None),
        attrs_fn=_meter_detail_attrs,
    ),
    MeterSensorDescription(
        key="meter_read_type",
        translation_key="meter_read_type",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: _meter_field(m, "detectedReadType"),
    ),
    MeterSensorDescription(
        key="meter_jurisdiction",
        translation_key="meter_jurisdiction",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: _meter_field(m, "jurisdictionCode"),
    ),
    MeterSensorDescription(
        key="meter_classification",
        translation_key="meter_classification",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: (str(_meter_field(m, "servicePointClassification") or "").lower() or None),
    ),
    MeterSensorDescription(
        key="meter_generator",
        translation_key="meter_generator",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.ENUM,
        options=["yes", "no"],
        value_fn=_generator,
    ),
    MeterSensorDescription(
        key="meter_nmi",
        translation_key="meter_nmi",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda m: _meter_field(m, "nationalMeteringId"),
    ),
)


METER_SENSORS: tuple[MeterSensorDescription, ...] = (
    *METER_DETAIL_SENSORS,
    _day_sensor(CONSUMPTION),
    _day_sensor(EXPORT),
    _day_sensor(CONTROLLED_LOAD),
    MeterSensorDescription(
        key="der_approved_capacity",
        translation_key="der_approved_capacity",
        device_class=SensorDeviceClass.APPARENT_POWER,
        native_unit_of_measurement=UnitOfApparentPower.KILO_VOLT_AMPERE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: _num(_der(m, "approvedCapacity")),
        attrs_fn=lambda m: {
            "installed_phases": _der(m, "installedPhasesCount"),
            "available_phases": _der(m, "availablePhasesCount"),
            "islandable": _der(m, "islandableInstallation"),
            "ac_connections": _der(m, "acConnections"),
        },
    ),
    MeterSensorDescription(
        key="der_export_limit",
        translation_key="der_export_limit",
        device_class=SensorDeviceClass.APPARENT_POWER,
        native_unit_of_measurement=UnitOfApparentPower.KILO_VOLT_AMPERE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: _num(((m.der or {}).get("protectionMode") or {}).get("exportLimitKva")),
    ),
    _period_sensor(CONSUMPTION, "month_to_date_import", "month"),
    _period_sensor(EXPORT, "month_to_date_export", "month"),
    _period_sensor(CONSUMPTION, "last_30_days_import", "30d"),
    _period_sensor(EXPORT, "last_30_days_export", "30d"),
    MeterSensorDescription(
        key="usage_history_days",
        translation_key="usage_history_days",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement="d",
        value_fn=lambda m: len({d for v in m.daily_history.values() for d in v}) or None,
        attrs_fn=_history_attrs,
        exists_fn=lambda m: bool(m.daily_history),
    ),
    _peak_sensor(CONSUMPTION, "latest_day_peak_import"),
    _peak_sensor(EXPORT, "latest_day_peak_export"),
    MeterSensorDescription(
        key="der_solar_capacity",
        translation_key="der_solar_capacity",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.der_summary.active_pv_kw if m.der_summary else None,
        attrs_fn=_der_summary_attrs,
        exists_fn=lambda m: m.der_summary is not None,
    ),
    MeterSensorDescription(
        key="der_register_issues",
        translation_key="der_register_issues",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: len(m.der_summary.issues) if m.der_summary else None,
        attrs_fn=lambda m: {
            "issues": m.der_summary.issues if m.der_summary else [],
            "how_to_fix": "Ask your network (or installer) to lodge a correction to the DER Register",
        },
        exists_fn=lambda m: m.der_summary is not None,
    ),
    MeterSensorDescription(
        key="substituted_intervals",
        translation_key="substituted_intervals",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.usage.substituted_intervals if m.usage else None,
    ),
    MeterSensorDescription(
        key="usage_fetched",
        translation_key="usage_fetched",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.usage_fetched_at,
        attrs_fn=lambda m: {
            "usage_status": m.usage_status,
            "der_status": m.der_status,
            "meters_source": m.meters_source,
            "oldest_usage_date": (
                m.usage.oldest_day.isoformat() if m.usage and m.usage.oldest_day else None
            ),
            "latest_usage_date": (
                m.usage.latest_complete_day.isoformat()
                if m.usage and m.usage.latest_complete_day
                else None
            ),
        },
    ),
)


DAY_KEYS = {f"latest_day_{s}" for s in (CONSUMPTION, EXPORT, CONTROLLED_LOAD)}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenfeedConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create sensors for every shared account and meter."""
    coordinator = entry.runtime_data
    data = coordinator.data
    entities: list[SensorEntity] = []
    registry = er.async_get(hass)
    stale: list[str] = []
    for account_id, account in data.energy.items():
        for d in (*ACCOUNT_SENSORS, *TARIFF_SENSORS):
            if d.exists_fn(account):
                entities.append(EnergyAccountSensor(coordinator, account_id, d))
            else:
                stale.append(f"{entry.unique_id}_{account_id}_{d.key}")
        for meter_id, meter in account.meters.items():
            series_seen = set(meter.usage.daily) if meter.usage else set()
            for d in METER_SENSORS:
                if not d.exists_fn(meter):
                    continue
                if d.key in DAY_KEYS and d.key[11:] not in series_seen:
                    if d.key != f"latest_day_{CONSUMPTION}":
                        continue
                if d.key.startswith("der_") and meter.der is None:
                    continue
                entities.append(MeterSensor(coordinator, account_id, meter_id, d))
    for account_id, bank in data.banking.items():
        entities.append(BankingBalanceSensor(coordinator, account_id, "current"))
        entities.append(BankingBalanceSensor(coordinator, account_id, "available"))
        for bd in BANK_SENSORS:
            if bd.exists_fn(coordinator, bank):
                entities.append(BankingSensor(coordinator, account_id, bd))
            else:
                stale.append(f"{entry.unique_id}_{account_id}_bank_{bd.key}")
    # Remove entities from earlier versions that this account cannot supply,
    # such as a plan sensor for a retailer that shares no plan.
    for unique_id in stale:
        if entity_id := registry.async_get_entity_id("sensor", DOMAIN, unique_id):
            registry.async_remove(entity_id)
    async_add_entities(entities)


def energy_device_info(coordinator: OpenfeedCoordinator, account_id: str) -> DeviceInfo:
    """The device shared by every entity for one energy account."""
    account = coordinator.data.energy[account_id].account
    number = str(account.get("accountNumber") or "")[-4:]
    return DeviceInfo(
        identifiers={(DOMAIN, f"energy_{account_id}")},
        name=f"{account.get('providerName', 'Energy')} {account.get('displayName') or number}".strip(),
        manufacturer=account.get("providerName"),
        model="Energy account (CDR)",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=DASHBOARD_URL,
    )


class _Base(CoordinatorEntity[OpenfeedCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_attribution = "Consumer Data Right data via openfeed"


class EnergyAccountSensor(_Base):
    entity_description: AccountSensorDescription

    def __init__(self, coordinator: OpenfeedCoordinator, account_id: str,
                 description: AccountSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._account_id = account_id
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{account_id}_{description.key}"
        self._attr_device_info = energy_device_info(coordinator, account_id)
        balance = coordinator.data.energy[account_id].balance or {}
        currency = balance.get("currency") or "AUD"
        if description.device_class == SensorDeviceClass.MONETARY:
            self._attr_native_unit_of_measurement = currency
        elif description.unit_suffix:
            self._attr_native_unit_of_measurement = f"{currency}/{description.unit_suffix}"
        elif description.key == "tariff_demand_charge":
            account = coordinator.data.energy[account_id]
            now = _tariff(account)
            unit = (now.demand_unit if now and now.demand_unit else "KVA").replace("KVA", "kVA")
            self._attr_native_unit_of_measurement = f"{currency}/{unit.replace('KW', 'kW')}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.entity_description.clock:
            self.async_on_remove(async_track_time_change(self.hass, self._tick, second=0))

    @callback
    def _tick(self, _now: datetime) -> None:
        self.async_write_ha_state()

    @property
    def _account(self) -> EnergyAccountData | None:
        return self.coordinator.data.energy.get(self._account_id)

    @property
    def available(self) -> bool:
        a = self._account
        return super().available and a is not None and self.entity_description.available_fn(a)

    @property
    def native_value(self) -> Any:
        a = self._account
        return self.entity_description.value_fn(a) if a else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        a = self._account
        return self.entity_description.attrs_fn(a) if a else {}


class MeterSensor(_Base):
    entity_description: MeterSensorDescription

    def __init__(self, coordinator: OpenfeedCoordinator, account_id: str, meter_id: str,
                 description: MeterSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._account_id = account_id
        self._meter_id = meter_id
        meter = coordinator.data.energy[account_id].meters[meter_id]
        nmi = meter.meter.get("nationalMeteringId")
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{meter.key}_{description.key}"
        # Legacy entries keep the NMI naming their entity IDs already use. On
        # newer entries the full NMI is only on the disabled-by-default NMI sensor.
        legacy = coordinator.meter_salt is None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"meter_{meter.key}")},
            name=f"Meter {nmi or meter.key}" if legacy else meter_label(meter.meter),
            model=f"Service point ({meter.meter.get('detectedReadType') or 'unknown'} reads)",
            serial_number=nmi if legacy else None,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=ENERGY_CONFIG_URL,
        )

    @property
    def _meter(self) -> MeterData | None:
        account = self.coordinator.data.energy.get(self._account_id)
        return account.meters.get(self._meter_id) if account else None

    @property
    def available(self) -> bool:
        return super().available and self._meter is not None

    @property
    def native_value(self) -> Any:
        m = self._meter
        value = self.entity_description.value_fn(m) if m else None
        if isinstance(value, datetime) or value is None:
            return value
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        m = self._meter
        if m is None:
            return {}
        return {
            "statistic_prefix": f"{DOMAIN}:{m.key}_",
            "jurisdiction": m.meter.get("jurisdictionCode"),
            **self.entity_description.attrs_fn(m),
        }


class BankingBalanceSensor(_Base):
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: OpenfeedCoordinator, account_id: str, kind: str) -> None:
        super().__init__(coordinator)
        self._account_id = account_id
        self._kind = kind
        self._attr_translation_key = f"banking_{kind}_balance"
        account = coordinator.data.banking[account_id].account
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{account_id}_bank_{kind}"
        self._attr_device_info = bank_device_info(account_id, account)

    @property
    def _item(self) -> BankingAccountData | None:
        return self.coordinator.data.banking.get(self._account_id)

    @property
    def available(self) -> bool:
        item = self._item
        return super().available and item is not None and item.balance is not None

    @property
    def native_unit_of_measurement(self) -> str | None:
        item = self._item
        return ((item.balance or {}).get("currency") if item else None) or "AUD"

    @property
    def native_value(self) -> float | None:
        item = self._item
        if item is None or item.balance is None:
            return None
        return _num(item.balance.get(f"{self._kind}Balance"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        item = self._item
        if item is None:
            return {}
        return {
            "masked_number": item.account.get("maskedNumber"),
            "account_type": item.account.get("accountType"),
            "credit_limit": (item.balance or {}).get("creditLimit"),
            "status": item.balance_status,
        }


# Banking account sensors ---------------------------------------------------


def bank_device_info(account_id: str, account: dict[str, Any]) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"bank_{account_id}")},
        name=f"{account.get('providerName', 'Bank')} {account.get('displayName', '')}".strip(),
        manufacturer=account.get("providerName"),
        model=account.get("productName") or "Banking account (CDR)",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=DASHBOARD_URL,
    )


@dataclass(frozen=True, kw_only=True)
class BankSensorDescription(SensorEntityDescription):
    value_fn: Callable[[BankingAccountData], Any]
    attrs_fn: Callable[[BankingAccountData], dict[str, Any]] = lambda _b: {}
    # Only create the entity when this returns True at setup.
    exists_fn: Callable[[OpenfeedCoordinator, BankingAccountData], bool] = lambda _c, _b: True
    # Money in the account currency (else the description's own unit).
    money: bool = False


def _flows(fn: Callable[[Any], Any]) -> Callable[[BankingAccountData], Any]:
    return lambda b: fn(b.flows) if b.flows else None


def _flow_attrs(b: BankingAccountData) -> dict[str, Any]:
    f = b.flows
    return {
        "status": b.flows_status,
        "fetched_at": b.flows_fetched_at.isoformat() if b.flows_fetched_at else None,
        "month_start": f.month_start.isoformat() if f and f.month_start else None,
        "latest_transaction_date": f.latest_day.isoformat() if f and f.latest_day else None,
        "posted_transactions": f.posted if f else None,
        "pending_transactions": f.pending if f else None,
        "skipped_transactions": f.skipped if f else None,
    }


def _wants_flows(c: OpenfeedCoordinator, _b: BankingAccountData) -> bool:
    return c.bank_transactions


def _rate_attrs(b: BankingAccountData) -> dict[str, Any]:
    r = b.rate
    detail = b.detail or {}
    return {
        "rate_kind": r.kind if r else None,
        "rate_type": r.rate_type if r else None,
        "all_rates": list(r.rates) if r else None,
        "product_name": detail.get("productName") or b.account.get("productName"),
        "fees_listed": len(detail.get("fees") or []),
        "detail_status": b.detail_status,
    }


BANK_SENSORS: tuple[BankSensorDescription, ...] = (
    BankSensorDescription(
        key="credit_limit",
        translation_key="banking_credit_limit",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        money=True,
        value_fn=lambda b: _num((b.balance or {}).get("creditLimit")),
        exists_fn=lambda _c, b: _num((b.balance or {}).get("creditLimit")) is not None,
    ),
    BankSensorDescription(
        key="interest_rate",
        translation_key="banking_interest_rate",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda b: b.rate.rate if b.rate else None,
        attrs_fn=_rate_attrs,
        exists_fn=lambda _c, b: b.rate is not None,
    ),
    BankSensorDescription(
        key="month_to_date_money_in",
        translation_key="banking_month_to_date_money_in",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        money=True,
        value_fn=_flows(lambda f: f.month_in),
        attrs_fn=_flow_attrs,
        exists_fn=_wants_flows,
    ),
    BankSensorDescription(
        key="month_to_date_money_out",
        translation_key="banking_month_to_date_money_out",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        money=True,
        value_fn=_flows(lambda f: f.month_out),
        attrs_fn=lambda b: {**_flow_attrs(b), "month_by_type": b.flows.by_type if b.flows else None},
        exists_fn=_wants_flows,
    ),
    BankSensorDescription(
        key="last_30_days_money_in",
        translation_key="banking_last_30_days_money_in",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        money=True,
        value_fn=_flows(lambda f: f.last_30_in),
        exists_fn=_wants_flows,
    ),
    BankSensorDescription(
        key="last_30_days_money_out",
        translation_key="banking_last_30_days_money_out",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        money=True,
        value_fn=_flows(lambda f: f.last_30_out),
        exists_fn=_wants_flows,
    ),
    BankSensorDescription(
        key="month_to_date_interest",
        translation_key="banking_month_to_date_interest",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        money=True,
        # Net interest: paid to you minus charged to you.
        value_fn=_flows(lambda f: round(f.month_interest_paid - f.month_interest_charged, 2)),
        attrs_fn=lambda b: {
            "interest_paid": b.flows.month_interest_paid if b.flows else None,
            "interest_charged": b.flows.month_interest_charged if b.flows else None,
        },
        exists_fn=_wants_flows,
    ),
    BankSensorDescription(
        key="month_to_date_fees",
        translation_key="banking_month_to_date_fees",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        money=True,
        value_fn=_flows(lambda f: f.month_fees),
        exists_fn=_wants_flows,
    ),
)


class BankingSensor(_Base):
    entity_description: BankSensorDescription

    def __init__(self, coordinator: OpenfeedCoordinator, account_id: str,
                 description: BankSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._account_id = account_id
        bank = coordinator.data.banking[account_id]
        self._attr_unique_id = (
            f"{coordinator.config_entry.unique_id}_{account_id}_bank_{description.key}"
        )
        self._attr_device_info = bank_device_info(account_id, bank.account)
        if description.money:
            self._attr_native_unit_of_measurement = (bank.balance or {}).get("currency") or "AUD"

    @property
    def _item(self) -> BankingAccountData | None:
        return self.coordinator.data.banking.get(self._account_id)

    @property
    def available(self) -> bool:
        item = self._item
        return (
            super().available
            and item is not None
            and self.entity_description.value_fn(item) is not None
        )

    @property
    def native_value(self) -> Any:
        item = self._item
        return None if item is None else self.entity_description.value_fn(item)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        item = self._item
        return {} if item is None else self.entity_description.attrs_fn(item)
