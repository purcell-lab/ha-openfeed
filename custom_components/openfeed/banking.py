"""Pure helpers for openfeed banking data.

Only totals leave this module. Transaction descriptions, references,
merchant and biller names are never kept, and the unmasked BSB and account
number from the account detail are dropped as soon as the detail arrives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

# Fields in the account detail that must never be stored or shown.
SENSITIVE_DETAIL_FIELDS = ("bsb", "accountNumber")

# CDS deposit rate types that add to a base rate rather than replace it.
BONUS_RATE_TYPES = {"BONUS", "BUNDLE_BONUS"}

# Transaction types that are summed into their own totals.
INTEREST_PAID = "INTEREST_PAID"
INTEREST_CHARGED = "INTEREST_CHARGED"
FEE = "FEE"


def strip_detail(detail: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the account detail without the unmasked BSB and account number."""
    if not isinstance(detail, dict):
        return None
    return {k: v for k, v in detail.items() if k not in SENSITIVE_DETAIL_FIELDS}


def _num(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True, frozen=True)
class AccountRate:
    """The headline interest rate for one account, in percent."""

    kind: str  # "deposit" or "lending"
    rate: float  # percent, e.g. 5.25
    rate_type: str | None
    # Every rate the provider lists, in percent, for the attributes.
    rates: tuple[dict[str, Any], ...] = ()


def _rate_percent(value: Any) -> float | None:
    """CDS RateString is a fraction (0.0525 means 5.25%)."""
    number = _num(value)
    return None if number is None else round(number * 100, 4)


def account_rate(detail: dict[str, Any] | None) -> AccountRate | None:
    """Pick the headline rate: lending for loans and cards, else the base deposit rate."""
    if not detail:
        return None
    for kind, key in (("lending", "lendingRates"), ("deposit", "depositRates")):
        listed = [r for r in detail.get(key) or [] if isinstance(r, dict)]
        rates = [
            {"rate": pct, "rate_type": r.get("rateType")}
            for r in listed
            if (pct := _rate_percent(r.get("rate"))) is not None
        ]
        if not rates:
            continue
        base = next((r for r in rates if r["rate_type"] not in BONUS_RATE_TYPES), rates[0])
        return AccountRate(kind=kind, rate=base["rate"], rate_type=base["rate_type"], rates=tuple(rates))
    return None


def transaction_day(txn: dict[str, Any]) -> date | None:
    """First usable date from the four date fields, in the order openfeed advises."""
    for key in ("transactionDate", "valueDate", "postedDateTime", "executionDateTime"):
        value = txn.get(key)
        if not value:
            continue
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            continue
    return None


def window_start(today: date) -> date:
    """Oldest date needed for month-to-date and last-30-day totals."""
    return min(today.replace(day=1), today - timedelta(days=29))


@dataclass(slots=True)
class FlowSummary:
    """Money in and out for one banking account, totals only."""

    month_in: float = 0.0
    month_out: float = 0.0
    last_30_in: float = 0.0
    last_30_out: float = 0.0
    month_interest_paid: float = 0.0
    month_interest_charged: float = 0.0
    month_fees: float = 0.0
    posted: int = 0
    pending: int = 0
    # Records with no usable date or amount; counted, never guessed.
    skipped: int = 0
    latest_day: date | None = None
    month_start: date | None = None
    by_type: dict[str, float] = field(default_factory=dict)


def summarise_transactions(txns: list[dict[str, Any]], today: date) -> FlowSummary:
    """Total posted transactions for this month and the last 30 days.

    Amounts follow openfeed: positive is money in, negative is money out.
    Pending transactions are counted but not summed, because they can change.
    """
    month_start = today.replace(day=1)
    last_30_start = today - timedelta(days=29)
    summary = FlowSummary(month_start=month_start)
    seen: set[str] = set()
    for txn in txns:
        txn_id = txn.get("transactionId")
        if txn_id:
            if txn_id in seen:
                continue
            seen.add(txn_id)
        if str(txn.get("status") or "").upper() == "PENDING":
            summary.pending += 1
            continue
        day = transaction_day(txn)
        amount = _num(txn.get("amount"))
        if day is None or amount is None:
            summary.skipped += 1
            continue
        if day > today:
            continue
        summary.posted += 1
        if summary.latest_day is None or day > summary.latest_day:
            summary.latest_day = day
        money_in, money_out = (amount, 0.0) if amount > 0 else (0.0, -amount)
        if day >= last_30_start:
            summary.last_30_in += money_in
            summary.last_30_out += money_out
        if day < month_start:
            continue
        summary.month_in += money_in
        summary.month_out += money_out
        kind = str(txn.get("transactionType") or "OTHER").upper()
        summary.by_type[kind] = summary.by_type.get(kind, 0.0) + amount
        if kind == INTEREST_PAID:
            summary.month_interest_paid += abs(amount)
        elif kind == INTEREST_CHARGED:
            summary.month_interest_charged += abs(amount)
        elif kind == FEE:
            summary.month_fees += abs(amount)
    for name in (
        "month_in", "month_out", "last_30_in", "last_30_out",
        "month_interest_paid", "month_interest_charged", "month_fees",
    ):
        setattr(summary, name, round(getattr(summary, name), 2))
    summary.by_type = {k: round(v, 2) for k, v in sorted(summary.by_type.items())}
    return summary
