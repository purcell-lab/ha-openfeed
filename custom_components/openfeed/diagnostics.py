"""Diagnostics for openfeed, with tokens, keys and identifiers redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_APP_KEY, CONF_DPOP_KEY, CONF_REDIRECT_BASE, CONF_SUBJECT, CONF_TOKEN
from .coordinator import OpenfeedConfigEntry

TO_REDACT = {
    CONF_APP_KEY,
    CONF_DPOP_KEY,
    CONF_TOKEN,
    CONF_SUBJECT,
    CONF_REDIRECT_BASE,
    "access_token",
    "refresh_token",
    "accountNumber",
    "bsb",
    "maskedNumber",
    "nationalMeteringId",
    "accountId",
    "meterId",
    "servicePointId",
    "invoiceNumber",
    "grant_id",
    "meter_key_salt",
    "client_id",
    "app_id",
    # Nested identifiers and free text that can identify a person or site.
    "meterIds",
    "connectionIdentifier",
    "deviceIdentifier",
    "displayName",
    "description",
    # The service point address in the meter detail (CDR location), and any
    # other address a data holder adds.
    "location",
    "address",
    "addressLine1",
    "addressLine2",
    "addressLine3",
    "postcode",
    "mailingAddress",
    "physicalAddress",
    "simple",
    "paf",
    # People on the account (CDR plans[].authorisedContacts).
    "authorisedContacts",
    "firstName",
    "lastName",
    "middleNames",
    "prefix",
    "suffix",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: OpenfeedConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics. Usage series are summarised, not dumped."""
    coordinator = entry.runtime_data
    data = coordinator.data
    energy = {}
    for index, account in enumerate(data.energy.values()):
        meters = []
        for meter in account.meters.values():
            usage = meter.usage
            meters.append(
                {
                    "meter": meter.meter,
                    "der": meter.der,
                    "der_issues": meter.der_summary.issues if meter.der_summary else None,
                    "usage": None
                    if usage is None
                    else {
                        "series": {s: len(v) for s, v in usage.hourly.items()},
                        "oldest_day": str(usage.oldest_day),
                        "latest_day": str(usage.latest_complete_day),
                        "power_series": {s: len(v) for s, v in usage.hourly_power.items()},
                        "substituted_intervals": usage.substituted_intervals,
                        "skipped_reads": usage.skipped_reads,
                        "interval_minutes": usage.interval_minutes,
                    },
                    "usage_fetched_at": str(meter.usage_fetched_at),
                    # What was asked for and what came back, per window and
                    # page, with interaction IDs for openfeed support.
                    "usage_requests": coordinator.usage_trace.get(meter.meter.get("meterId"), []),
                    "usage_status": meter.usage_status,
                    "der_status": meter.der_status,
                    "meters_source": meter.meters_source,
                    "meter_detail_source": meter.detail_source,
                }
            )
        b = account.billing
        energy[f"account_{index}"] = {
            "account": account.account,
            "balance_status": account.balance_status,
            "latest_invoice": account.latest_invoice,
            "plan_shared": account.plan is not None,
            "tariff_detail_shared": bool((account.plan or {}).get("planDetail")),
            "history": {
                "invoices": account.invoice_count,
                "oldest_invoice": str(b.oldest_invoice) if b else None,
                "billing_status": account.billing_status,
                "billing_transactions": b.transactions if b else None,
                "oldest_transaction": str(b.oldest_transaction) if b else None,
                "money_series": {s: len(v) for s, v in b.hourly.items()} if b else None,
                "series_totals": (
                    {s: round(sum(v.values()), 2) for s, v in b.hourly.items()} if b else None
                ),
            },
            # Date, type, amount and GST only; no descriptions, invoice or meter IDs.
            "billing_lines": b.lines if b else None,
            "meters": meters,
        }
    return async_redact_data(
        {
            "entry": {"data": dict(entry.data), "options": dict(entry.options)},
            "fetched_at": str(data.fetched_at),
            "energy": energy,
            "banking": {
                f"account_{i}": {
                    "account": b.account,
                    "balance_status": b.balance_status,
                    "detail_status": b.detail_status,
                    "rate": (
                        {"kind": b.rate.kind, "rate": b.rate.rate, "rate_type": b.rate.rate_type}
                        if b.rate
                        else None
                    ),
                    # Status and counts only; no amounts or transaction text.
                    "flows_status": b.flows_status,
                    "flows_posted": b.flows.posted if b.flows else None,
                    "flows_pending": b.flows.pending if b.flows else None,
                    "flows_skipped": b.flows.skipped if b.flows else None,
                }
                for i, b in enumerate(data.banking.values())
            },
            "last_update_success": coordinator.last_update_success,
        },
        TO_REDACT,
    )
