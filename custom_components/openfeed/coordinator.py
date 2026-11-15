"""Data update coordinator for openfeed."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
import secrets
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    BalanceUnavailableError,
    CreditExhaustedError,
    GrantRevokedError,
    OpenfeedApiError,
    RateLimitedError,
    TokenRejectedError,
    cancel_on_revocation,
)
from .banking import AccountRate, FlowSummary, account_rate, strip_detail, summarise_transactions, window_start
from .billing import PAYMENTS, REFUNDS, BillingSummary, summarise_billing
from .const import (
    ACCOUNT_DETAIL_REFRESH_HOURS,
    BANK_TRANSACTIONS_REFRESH_HOURS,
    BILLING_REFRESH_HOURS,
    CONF_BANK_TRANSACTIONS,
    CONF_BACKFILL_DAYS,
    CONF_METER_KEY_SALT,
    CONF_METER_KEY_SCHEME,
    CONF_MONEY_STATS_VERSION,
    CONF_SCOPES,
    DEFAULT_BACKFILL_DAYS,
    DOMAIN,
    METER_KEY_LEGACY,
    MONEY_STATS_VERSION,
    SCOPE_BANKING,
    SCOPE_ENERGY,
    UPDATE_INTERVAL_MINUTES,
    USAGE_REFRESH_HOURS,
    USAGE_RESTATE_DAYS,
    USAGE_WINDOW_DAYS,
)
from .oauth import OpenfeedAuthError
from .session import OpenfeedSession
from .der import DerSummary, summarise_der
from .statistics import (
    account_key,
    async_clear,
    async_import_meter,
    async_import_money,
    async_import_power,
    meter_key,
    meter_label,
    statistic_id,
)
from .usage import AEST, UsageSummary, summarise, usage_windows

# Keep enough merged daily totals for 30-day and month-to-date sensors.
HISTORY_KEEP_DAYS = 400

_LOGGER = logging.getLogger(__name__)

ISSUE_CREDITS = "credit_exhausted"

# Errors that end the whole refresh. Anything else from one meter's detail,
# DER or usage call is contained to that meter, and its last good data is kept.
FATAL_ERRORS = (GrantRevokedError, TokenRejectedError, CreditExhaustedError, RateLimitedError)


def _contained(err: Exception) -> bool:
    """True for an API error that should only affect one meter."""
    return isinstance(err, OpenfeedApiError) and not isinstance(err, FATAL_ERRORS)


@dataclass(slots=True)
class MeterData:
    """One service point and what the integration knows about it."""

    account_id: str
    meter: dict[str, Any]
    key: str
    # "detail" when the meter detail endpoint answered, else "list".
    detail_source: str = "list"
    detail_fetched_at: datetime | None = None
    der: dict[str, Any] | None = None
    der_summary: DerSummary | None = None
    usage: UsageSummary | None = None
    usage_fetched_at: datetime | None = None
    # series -> AEST date -> kWh, merged across refreshes (backfill plus restatements)
    daily_history: dict[str, dict[date, float]] = field(default_factory=dict)
    # "ok", or "error" when the last call failed and older data (if any) is shown.
    usage_status: str = "ok"
    der_status: str = "ok"
    # "list" when the meters list failed and the last good meter data is shown.
    meters_source: str = "list"


@dataclass(slots=True)
class EnergyAccountData:
    """One energy account."""

    account: dict[str, Any]
    balance: dict[str, Any] | None = None
    balance_status: str = "ok"
    latest_invoice: dict[str, Any] | None = None
    invoices: list[dict[str, Any]] = field(default_factory=list)
    invoice_count: int = 0
    billing: BillingSummary | None = None
    billing_status: str = "not_fetched"
    billing_fetched_at: datetime | None = None
    meters: dict[str, MeterData] = field(default_factory=dict)

    @property
    def plan(self) -> dict[str, Any] | None:
        plans = self.account.get("plans") or []
        return plans[0] if plans else None


@dataclass(slots=True)
class BankingAccountData:
    """One banking account."""

    account: dict[str, Any]
    balance: dict[str, Any] | None = None
    balance_status: str = "ok"
    # Account detail without the unmasked BSB and account number.
    detail: dict[str, Any] | None = None
    detail_status: str = "not_fetched"
    rate: AccountRate | None = None
    # Totals from transactions, only when the user opts in.
    flows: FlowSummary | None = None
    flows_status: str = "off"
    flows_fetched_at: datetime | None = None


@dataclass(slots=True)
class OpenfeedData:
    """Everything one refresh produced."""

    energy: dict[str, EnergyAccountData] = field(default_factory=dict)
    banking: dict[str, BankingAccountData] = field(default_factory=dict)
    fetched_at: datetime | None = None


type OpenfeedConfigEntry = ConfigEntry[OpenfeedCoordinator]


def _latest_invoice(invoices: list[dict[str, Any]]) -> dict[str, Any] | None:
    dated = [i for i in invoices if i.get("issueDate")]
    if not dated:
        return invoices[-1] if invoices else None
    return max(dated, key=lambda i: str(i["issueDate"]))


class OpenfeedCoordinator(DataUpdateCoordinator[OpenfeedData]):
    """Polls the Sharing API and imports usage statistics."""

    config_entry: OpenfeedConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, session: OpenfeedSession) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.session = session
        self._ensure_meter_salt()
        self._usage_cache: dict[str, MeterData] = {}
        self._backfilled: set[str] = set()
        self._money_migrated = False
        # meter ID -> series -> AEST date -> kWh, merged across refreshes
        self._daily_history: dict[str, dict[str, dict[date, float]]] = {}
        # account ID -> (fetched at, plans list or None)
        self._plan_cache: dict[str, tuple[datetime, list[dict[str, Any]] | None]] = {}
        # meter ID -> (fetched at, meter detail or None)
        self._meter_detail_cache: dict[str, tuple[datetime, dict[str, Any] | None]] = {}
        # account ID -> (fetched at, status, summary)
        self._billing_cache: dict[str, tuple[datetime, str, BillingSummary | None]] = {}
        # bank account ID -> (fetched at, detail without BSB/account number, or None)
        self._bank_detail_cache: dict[str, tuple[datetime, dict[str, Any] | None]] = {}
        # bank account ID -> (fetched at, status, totals)
        self._bank_flow_cache: dict[str, tuple[datetime, str, FlowSummary | None]] = {}

    def _ensure_meter_salt(self) -> None:
        """Give a new (non-legacy) entry its meter key salt, once, for good."""
        data = self.config_entry.data
        if data.get(CONF_METER_KEY_SCHEME) == METER_KEY_LEGACY or data.get(CONF_METER_KEY_SALT):
            return
        self.hass.config_entries.async_update_entry(
            self.config_entry, data={**data, CONF_METER_KEY_SALT: secrets.token_hex(16)}
        )

    @property
    def meter_salt(self) -> str | None:
        """The per-entry salt for meter keys, or None for legacy NMI keys."""
        data = self.config_entry.data
        if data.get(CONF_METER_KEY_SCHEME) == METER_KEY_LEGACY:
            return None
        return data.get(CONF_METER_KEY_SALT)

    @property
    def bank_transactions(self) -> bool:
        """True when the user opted in to transaction totals."""
        return bool(self.config_entry.options.get(CONF_BANK_TRANSACTIONS, False))

    @property
    def scopes(self) -> list[str]:
        return list(self.config_entry.data.get(CONF_SCOPES, [SCOPE_ENERGY]))

    async def _async_update_data(self) -> OpenfeedData:
        try:
            try:
                data = await self._collect()
            except TokenRejectedError:
                await self.session.async_force_refresh()
                data = await self._collect()
        except GrantRevokedError as err:
            # Revocation is a hard stop: purge cached data and the grant,
            # then ask the user to reconnect.
            self._usage_cache.clear()
            self._daily_history.clear()
            self._plan_cache.clear()
            self._meter_detail_cache.clear()
            self._billing_cache.clear()
            self._bank_detail_cache.clear()
            self._bank_flow_cache.clear()
            self.session.clear_grant()
            raise ConfigEntryAuthFailed(
                "The openfeed disclosure grant was revoked or has ended"
            ) from err
        except TokenRejectedError as err:
            raise ConfigEntryAuthFailed("openfeed rejected a fresh access token") from err
        except CreditExhaustedError as err:
            ir.async_create_issue(
                self.hass, DOMAIN, ISSUE_CREDITS, is_fixable=False,
                severity=ir.IssueSeverity.ERROR, translation_key=ISSUE_CREDITS,
            )
            raise UpdateFailed("openfeed developer credits are exhausted; grant suspended") from err
        except RateLimitedError as err:
            raise UpdateFailed("openfeed rate limit reached; backing off") from err
        except (OpenfeedApiError, OpenfeedAuthError) as err:
            raise UpdateFailed(f"openfeed request failed: {err}") from err
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_CREDITS)
        if self._money_migrated:
            self._money_migrated = False
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_MONEY_STATS_VERSION: MONEY_STATS_VERSION},
            )
        return data

    async def _collect(self) -> OpenfeedData:
        data = OpenfeedData(fetched_at=dt_util.utcnow())
        client = self.session.client
        if SCOPE_ENERGY in self.scopes:
            for account in await client.energy_accounts():
                data.energy[account["accountId"]] = await self._collect_energy_account(account)
        if SCOPE_BANKING in self.scopes:
            accounts = await client.banking_accounts()
            balances = await cancel_on_revocation(
                [client.banking_balance(a["accountId"]) for a in accounts]
            )
            for account, balance in zip(accounts, balances, strict=True):
                item = BankingAccountData(account=account)
                _apply_balance(item, balance)
                await self._apply_bank_detail(item)
                if self.bank_transactions:
                    await self._apply_bank_flows(item)
                data.banking[account["accountId"]] = item
        return data

    async def _collect_energy_account(self, account: dict[str, Any]) -> EnergyAccountData:
        client = self.session.client
        account_id = account["accountId"]
        account = await self._with_plans(account)
        item = EnergyAccountData(account=account)
        balance, meters, invoices = await cancel_on_revocation(
            [
                client.energy_balance(account_id),
                client.energy_meters(account_id),
                client.energy_invoices(account_id),
            ]
        )
        _apply_balance(item, balance)
        if isinstance(invoices, list):
            item.latest_invoice = _latest_invoice(invoices)
            item.invoice_count = len(invoices)
            item.invoices = invoices
        await self._apply_billing(item, invoices if isinstance(invoices, list) else [])
        if isinstance(meters, OpenfeedApiError):
            cached = [md for md in self._usage_cache.values() if md.account_id == account_id]
            if isinstance(meters, FATAL_ERRORS) or not cached:
                raise meters
            # Keep showing the meters from the last good refresh.
            _LOGGER.warning(
                "openfeed meters list failed (%s); keeping the last meter data", meters
            )
            for md in cached:
                md.meters_source = "cache"
                item.meters[md.meter["meterId"]] = md
            return item
        for meter in meters:
            item.meters[meter["meterId"]] = await self._collect_meter(account_id, meter)
        return item

    async def _with_plans(self, account: dict[str, Any]) -> dict[str, Any]:
        """Fill plans from the account detail endpoint when the list omits them."""
        if account.get("plans"):
            return account
        account_id = account["accountId"]
        now = dt_util.utcnow()
        cached = self._plan_cache.get(account_id)
        if cached is None or now - cached[0] >= timedelta(hours=ACCOUNT_DETAIL_REFRESH_HOURS):
            (detail,) = await cancel_on_revocation(
                [self.session.client.energy_account(account_id)]
            )
            plans = None
            if isinstance(detail, dict):
                plans = detail.get("plans") or None
            cached = (now, plans)
            self._plan_cache[account_id] = cached
        return {**account, "plans": cached[1]} if cached[1] else account

    async def _with_meter_detail(self, md: MeterData) -> None:
        """Merge the meter detail endpoint over the list entry, once a day."""
        meter_id = md.meter["meterId"]
        now = dt_util.utcnow()
        cached = self._meter_detail_cache.get(meter_id)
        if cached is None or now - cached[0] >= timedelta(hours=ACCOUNT_DETAIL_REFRESH_HOURS):
            (detail,) = await cancel_on_revocation(
                [self.session.client.energy_meter(md.account_id, meter_id)]
            )
            cached = (now, detail if isinstance(detail, dict) else None)
            self._meter_detail_cache[meter_id] = cached
        if cached[1] is not None:
            extra = {k: v for k, v in cached[1].items() if v is not None}
            md.meter = {**md.meter, **extra}
            md.detail_source = "detail"
            md.detail_fetched_at = cached[0]

    async def _apply_billing(
        self, item: EnergyAccountData, invoices: list[dict[str, Any]]
    ) -> None:
        """Refresh billing transactions and money statistics every few hours."""
        account_id = item.account["accountId"]
        now = dt_util.utcnow()
        cached = self._billing_cache.get(account_id)
        if cached and now - cached[0] < timedelta(hours=BILLING_REFRESH_HOURS):
            item.billing_fetched_at, item.billing_status, item.billing = cached
            return
        (transactions,) = await cancel_on_revocation(
            [self.session.client.energy_billing(account_id)]
        )
        status = "ok"
        if isinstance(transactions, OpenfeedApiError):
            status = "unavailable"
            transactions = []
        summary = summarise_billing(transactions, invoices, dt_util.now(AEST).date())
        if status == "ok" and not summary.transactions:
            status = "empty"
        rebuild: frozenset[str] = frozenset()
        if status == "ok" and self.config_entry.data.get(CONF_MONEY_STATS_VERSION, 1) < MONEY_STATS_VERSION:
            # Before v0.1.4 the payments series held signed payments and
            # refunds together, and v0.1.4 could carry a stale running sum.
            # Clear both series and rebuild them from zero.
            key = account_key(account_id)
            rebuild = frozenset({PAYMENTS, REFUNDS})
            await async_clear(self.hass, [statistic_id(key, s) for s in sorted(rebuild)])
            self._money_migrated = True
        if summary.hourly:
            currency = (item.balance or {}).get("currency") or "AUD"
            label = f"openfeed {item.account.get('providerName') or 'energy'} account"
            await async_import_money(
                self.hass, account_key(account_id), label, summary.hourly, currency, rebuild
            )
        self._billing_cache[account_id] = (now, status, summary)
        item.billing_fetched_at, item.billing_status, item.billing = now, status, summary

    async def _apply_bank_detail(self, item: BankingAccountData) -> None:
        """Fetch the account detail once a day, for rates and fees."""
        account_id = item.account["accountId"]
        now = dt_util.utcnow()
        cached = self._bank_detail_cache.get(account_id)
        status = "cache"
        if cached is None or now - cached[0] >= timedelta(hours=ACCOUNT_DETAIL_REFRESH_HOURS):
            (detail,) = await cancel_on_revocation(
                [self.session.client.banking_account(account_id)]
            )
            if isinstance(detail, OpenfeedApiError):
                if not _contained(detail):
                    raise detail
                _LOGGER.warning("openfeed bank account detail request failed: %s", detail)
                status = "error"
                # Keep the last good detail; try again on the next poll.
                if cached is None:
                    item.detail_status = status
                    return
            else:
                cached = (now, strip_detail(detail))
                self._bank_detail_cache[account_id] = cached
                status = "ok"
        item.detail = cached[1]
        item.detail_status = status
        item.rate = account_rate(cached[1])

    async def _apply_bank_flows(self, item: BankingAccountData) -> None:
        """Total recent transactions every few hours. Only totals are kept."""
        account_id = item.account["accountId"]
        now = dt_util.utcnow()
        cached = self._bank_flow_cache.get(account_id)
        if cached and cached[1] == "ok" and now - cached[0] < timedelta(
            hours=BANK_TRANSACTIONS_REFRESH_HOURS
        ):
            item.flows_fetched_at, item.flows_status, item.flows = cached
            return
        today = dt_util.now(AEST).date()
        (txns,) = await cancel_on_revocation(
            [
                self.session.client.banking_transactions(
                    account_id, window_start(today).isoformat(), today.isoformat()
                )
            ]
        )
        if isinstance(txns, OpenfeedApiError):
            if not _contained(txns):
                raise txns
            _LOGGER.warning("openfeed bank transactions request failed: %s", txns)
            if cached and cached[2] is not None:
                item.flows_fetched_at, item.flows = cached[0], cached[2]
            item.flows_status = "error"
            return
        summary = summarise_transactions(txns, today)
        self._bank_flow_cache[account_id] = (now, "ok", summary)
        item.flows_fetched_at, item.flows_status, item.flows = now, "ok", summary

    async def _collect_meter(self, account_id: str, meter: dict[str, Any]) -> MeterData:
        meter_id = meter["meterId"]
        cached = self._usage_cache.get(meter_id)
        md = MeterData(account_id=account_id, meter=meter, key=meter_key(meter, self.meter_salt))
        await self._with_meter_detail(md)
        try:
            md.der = await self.session.client.energy_der(account_id, meter_id)
        except OpenfeedApiError as err:
            if not _contained(err):
                raise
            _LOGGER.warning("openfeed DER request for a meter failed: %s", err)
            md.der = cached.der if cached else None
            md.der_status = "error"
        md.der_summary = summarise_der(md.der)
        now = dt_util.utcnow()
        # A failed usage refresh is retried on the next poll, not in 3 hours.
        if (
            cached
            and cached.usage_status == "ok"
            and cached.usage_fetched_at
            and now - cached.usage_fetched_at < timedelta(hours=USAGE_REFRESH_HOURS)
        ):
            md.usage, md.usage_fetched_at = cached.usage, cached.usage_fetched_at
        else:
            try:
                md.usage = await self._refresh_usage(md)
                md.usage_fetched_at = now
            except OpenfeedApiError as err:
                if not _contained(err):
                    raise
                # Keep the last good summary and when it was fetched.
                _LOGGER.warning("openfeed usage request for a meter failed: %s", err)
                md.usage = cached.usage if cached else None
                md.usage_fetched_at = cached.usage_fetched_at if cached else None
                md.usage_status = "error"
        md.daily_history = self._daily_history.get(meter_id, {})
        self._usage_cache[meter_id] = md
        return md

    async def _refresh_usage(self, md: MeterData) -> UsageSummary:
        today = dt_util.now(AEST).date()
        days = (
            USAGE_RESTATE_DAYS
            if md.meter["meterId"] in self._backfilled
            else int(self.config_entry.options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS))
        )
        oldest = today - timedelta(days=days)
        records: list[dict[str, Any]] = []
        for start, end in usage_windows(oldest, today, USAGE_WINDOW_DAYS):
            records.extend(
                await self.session.client.energy_usage(
                    md.account_id, md.meter["meterId"], start.isoformat(), end.isoformat()
                )
            )
        summary = summarise(records)
        history = self._daily_history.setdefault(md.meter["meterId"], {})
        cutoff = today - timedelta(days=HISTORY_KEEP_DAYS)
        for series, days_kwh in summary.daily.items():
            merged = history.setdefault(series, {})
            merged.update(days_kwh)
            for day in [d for d in merged if d < cutoff]:
                del merged[day]
        if summary.hourly:
            label = f"openfeed {meter_label(md.meter).lower()}"
            await async_import_meter(self.hass, md.key, label, summary.hourly)
        if summary.hourly_power:
            label = f"openfeed {meter_label(md.meter).lower()}"
            await async_import_power(self.hass, md.key, label, summary.hourly_power)
        self._backfilled.add(md.meter["meterId"])
        return summary


def _apply_balance(item: EnergyAccountData | BankingAccountData, result: Any) -> None:
    if isinstance(result, BalanceUnavailableError):
        item.balance_status = "unavailable"
    elif isinstance(result, OpenfeedApiError):
        item.balance_status = "error"
    else:
        item.balance = result


def period_total(
    history: dict[str, dict[date, float]],
    series: str,
    start: date,
    end: date,
) -> tuple[float, int] | None:
    """kWh and days with data for start <= day < end, or None with no data."""
    days = {d: v for d, v in (history.get(series) or {}).items() if start <= d < end}
    if not days:
        return None
    return round(sum(days.values()), 3), len(days)


def latest_day_peak(
    usage: UsageSummary | None, series: str, today: date | None = None
) -> tuple[date, float, datetime] | None:
    """Peak interval power (kW) on the most recent full AEST day."""
    if usage is None or not usage.daily_peak.get(series):
        return None
    today = today or dt_util.now(AEST).date()
    days = [d for d in usage.daily_peak[series] if d < today]
    if not days:
        return None
    day = max(days)
    kw, start = usage.daily_peak[series][day]
    return day, kw, start


def latest_day_total(
    usage: UsageSummary | None, series: str, today: date | None = None
) -> tuple[date, float] | None:
    """The most recent full AEST day total for a series (today excluded)."""
    if usage is None or not usage.daily.get(series):
        return None
    today = today or dt_util.now(AEST).date()
    days = [d for d in usage.daily[series] if d < today]
    if not days:
        return None
    day = max(days)
    return day, round(usage.daily[series][day], 3)
