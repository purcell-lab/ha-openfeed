"""Client for the openfeed Sharing API.

Every call is DPoP bound: Authorization: DPoP <token> plus a fresh proof
carrying the access token hash. Errors are mapped onto the openfeed error
taxonomy so the coordinator can tell a revoked grant from a provider that
cannot supply a live balance.

This module has no Home Assistant imports so it can be unit tested alone.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlsplit

import aiohttp
from cryptography.hazmat.primitives.asymmetric import rsa
from yarl import URL

from .crypto import dpop_proof

TokenGetter = Callable[[], Awaitable[str]]

# A collection longer than this many pages is treated as a server fault.
MAX_PAGES = 100


class OpenfeedApiError(Exception):
    """Base error for Sharing API calls."""

    def __init__(self, status: int, code: str | None = None, message: str | None = None):
        super().__init__(f"HTTP {status} {code or ''} {message or ''}".strip())
        self.status = status
        self.code = code
        self.message = message


class TokenRejectedError(OpenfeedApiError):
    """401: the access token is missing, expired or not bound to this key."""


class CreditExhaustedError(OpenfeedApiError):
    """402: the developer credit balance is exhausted and the grant is suspended."""


class GrantRevokedError(OpenfeedApiError):
    """403 disclosure_grant_required: the grant is absent or revoked."""


class BalanceUnavailableError(OpenfeedApiError):
    """403 balance_unavailable or 502: the provider cannot supply a live balance.

    A provider limitation, never a reason to purge the session.
    """


class NotFoundError(OpenfeedApiError):
    """404: the resource does not exist (for DER, no solar or battery)."""


class RateLimitedError(OpenfeedApiError):
    """429: back off."""


class EnforcementError(OpenfeedApiError):
    """403 with an empty body: a transient enforcement failure."""


def same_origin(url: str, base: str) -> bool:
    """True when url has the same scheme, host and port as base."""
    a, b = urlsplit(url), urlsplit(base)
    return (a.scheme.lower(), a.hostname, a.port) == (b.scheme.lower(), b.hostname, b.port)


def path_segment(value: str) -> str:
    """Quote an ID for a URL path, leaving '=' raw as openfeed requires."""
    return quote(value, safe="=-_.~")


class OpenfeedClient:
    """Thin async client for the Sharing API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        dpop_key: rsa.RSAPrivateKey,
        get_access_token: TokenGetter,
        api_base: str,
        nonces: dict[str, str] | None = None,
    ) -> None:
        self._session = session
        self._dpop_key = dpop_key
        self._get_token = get_access_token
        self._base = api_base.rstrip("/")
        self._nonces = nonces if nonces is not None else {}

    async def _request(self, method: str, url: str) -> dict[str, Any]:
        """Send one DPoP-bound request with nonce and enforcement retries."""
        retried_nonce = False
        retried_enforcement = False
        while True:
            token = await self._get_token()
            proof = dpop_proof(
                self._dpop_key, method, url, access_token=token,
                nonce=self._nonces.get(self._base),
            )
            headers = {"Authorization": f"DPoP {token}", "DPoP": proof, "Accept": "application/json"}
            async with self._session.request(
                method, URL(url, encoded=True), headers=headers
            ) as resp:
                if nonce := resp.headers.get("DPoP-Nonce"):
                    self._nonces[self._base] = nonce
                body: dict[str, Any] = {}
                text = await resp.text()
                if text:
                    try:
                        parsed = await resp.json(content_type=None)
                        body = parsed if isinstance(parsed, dict) else {"data": parsed}
                    except ValueError:
                        body = {}
                if resp.status == 200:
                    return body
                if resp.status == 204:
                    return {}
                www = resp.headers.get("WWW-Authenticate", "")
                if resp.status == 401 and "use_dpop_nonce" in www and nonce and not retried_nonce:
                    retried_nonce = True
                    continue
                if resp.status == 403 and not text and not retried_enforcement:
                    retried_enforcement = True
                    continue
                raise _map_error(resp.status, body, text)

    def _url(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = f"{self._base}{path}"
        if params:
            url = f"{url}?{urlencode({k: v for k, v in params.items() if v is not None})}"
        return url

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a single resource and unwrap the V1 envelope."""
        body = await self._request("GET", self._url(path, params))
        return body.get("data")

    async def get_collection(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """GET every page of a collection, following links.next to the end.

        The access token goes with every page, so a next link is only
        followed when it stays on the API origin. A link that repeats, or a
        collection longer than MAX_PAGES, stops with an error.
        """
        url: str | None = self._url(path, {"limit": 1000, **(params or {})})
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        while url:
            if url in seen or len(seen) >= MAX_PAGES:
                raise OpenfeedApiError(0, "invalid_next_link", "pagination did not end")
            seen.add(url)
            body = await self._request("GET", url)
            data = body.get("data") or []
            items.extend(data if isinstance(data, list) else [data])
            url = self._next_url(url, (body.get("links") or {}).get("next"))
        return items

    def _next_url(self, current: str, link: Any) -> str | None:
        """Resolve links.next and refuse any URL off the API origin."""
        if not link:
            return None
        resolved = urljoin(current, str(link))
        if not same_origin(resolved, self._base):
            raise OpenfeedApiError(0, "invalid_next_link", "next link leaves the API origin")
        return resolved

    # Energy ---------------------------------------------------------------

    async def energy_accounts(self) -> list[dict[str, Any]]:
        return await self.get_collection("/v1/energy/accounts")

    async def energy_account(self, account_id: str) -> dict[str, Any]:
        return await self.get(f"/v1/energy/accounts/{path_segment(account_id)}")

    async def energy_balance(self, account_id: str) -> dict[str, Any]:
        return await self.get(f"/v1/energy/accounts/{path_segment(account_id)}/balance")

    async def energy_meters(self, account_id: str) -> list[dict[str, Any]]:
        return await self.get_collection(f"/v1/energy/accounts/{path_segment(account_id)}/meters")

    async def energy_meter(self, account_id: str, meter_id: str) -> dict[str, Any]:
        return await self.get(
            f"/v1/energy/accounts/{path_segment(account_id)}/meters/{path_segment(meter_id)}"
        )

    async def energy_usage(
        self, account_id: str, meter_id: str, oldest: str, newest: str
    ) -> list[dict[str, Any]]:
        return await self.get_collection(
            f"/v1/energy/accounts/{path_segment(account_id)}/meters/{path_segment(meter_id)}/usage",
            {"oldestDate": oldest, "newestDate": newest},
        )

    async def energy_der(self, account_id: str, meter_id: str) -> dict[str, Any] | None:
        try:
            return await self.get(
                f"/v1/energy/accounts/{path_segment(account_id)}/meters/{path_segment(meter_id)}/der"
            )
        except NotFoundError:
            return None

    async def energy_invoices(self, account_id: str) -> list[dict[str, Any]]:
        return await self.get_collection(f"/v1/energy/accounts/{path_segment(account_id)}/invoices")

    async def energy_billing(self, account_id: str) -> list[dict[str, Any]]:
        return await self.get_collection(f"/v1/energy/accounts/{path_segment(account_id)}/billing")

    # Banking --------------------------------------------------------------

    async def banking_accounts(self) -> list[dict[str, Any]]:
        return await self.get_collection("/v1/banking/accounts")

    async def banking_balance(self, account_id: str) -> dict[str, Any]:
        return await self.get(f"/v1/banking/accounts/{path_segment(account_id)}/balance")

    async def banking_account(self, account_id: str) -> dict[str, Any] | None:
        """Account detail (rates, fees). The caller drops the BSB and account number."""
        return await self.get(f"/v1/banking/accounts/{path_segment(account_id)}")

    async def banking_transactions(
        self, account_id: str, oldest: str, newest: str
    ) -> list[dict[str, Any]]:
        return await self.get_collection(
            f"/v1/banking/accounts/{path_segment(account_id)}/transactions",
            {"oldestDate": oldest, "newestDate": newest},
        )


def _map_error(status: int, body: dict[str, Any], text: str) -> OpenfeedApiError:
    code = body.get("code") or body.get("error")
    message = body.get("message") or body.get("error_description")
    if status == 401:
        return TokenRejectedError(status, code, message)
    if status == 402:
        return CreditExhaustedError(status, code, message)
    if status == 403:
        if code == "disclosure_grant_required":
            return GrantRevokedError(status, code, message)
        if code == "balance_unavailable":
            return BalanceUnavailableError(status, code, message)
        if not text:
            return EnforcementError(status, code, message)
        return OpenfeedApiError(status, code, message)
    if status == 404:
        return NotFoundError(status, code, message)
    if status == 429:
        return RateLimitedError(status, code, message)
    if status == 502 and code == "balance_temporarily_unavailable":
        return BalanceUnavailableError(status, code, message)
    return OpenfeedApiError(status, code, message)


async def cancel_on_revocation(coros: list[Awaitable[Any]]) -> list[Any]:
    """Run sibling calls; a revocation in any one cancels the rest.

    Continuing to collect data after a revocation would breach the CDR
    rules, so a GrantRevokedError aborts every in-flight sibling. Other
    errors are returned in place so one failed balance cannot fail the lot.
    """
    async def guard(coro: Awaitable[Any]) -> Any:
        try:
            return await coro
        except GrantRevokedError:
            raise
        except OpenfeedApiError as err:
            return err

    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(guard(c)) for c in coros]
    except ExceptionGroup as group_err:
        revoked = group_err.subgroup(GrantRevokedError)
        if revoked is not None:
            raise revoked.exceptions[0] from None
        raise
    return [t.result() for t in tasks]
