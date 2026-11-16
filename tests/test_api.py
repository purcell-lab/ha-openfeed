"""Sharing API client: error mapping, pagination, revocation."""

from __future__ import annotations

import asyncio

import aiohttp
import jwt
import pytest

from custom_components.openfeed import api
from custom_components.openfeed.api import OpenfeedClient



async def _token():
    return "at"


def _client(session, dpop_key, base):
    return OpenfeedClient(session, dpop_key, _token, base)


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (401, {"error": "invalid_token"}, api.TokenRejectedError),
        (402, {"code": "credit_exhausted"}, api.CreditExhaustedError),
        (403, {"code": "disclosure_grant_required"}, api.GrantRevokedError),
        (403, {"code": "balance_unavailable"}, api.BalanceUnavailableError),
        (404, {"code": "not_found"}, api.NotFoundError),
        (429, {"code": "rate_limited"}, api.RateLimitedError),
        (502, {"code": "balance_temporarily_unavailable"}, api.BalanceUnavailableError),
    ],
)
async def test_error_mapping(fake_server, dpop_key, status, payload, expected):
    fake_server.add("GET", "/v1/energy/accounts/a/balance", status=status, json=payload)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(expected):
            await _client(session, dpop_key, fake_server.url).energy_balance("a")


async def test_empty_403_retried_once(fake_server, dpop_key):
    fake_server.add("GET", "/v1/energy/accounts/a/balance", status=403, body="")
    fake_server.add("GET", "/v1/energy/accounts/a/balance", json={"data": {"balance": "1.00"}})
    async with aiohttp.ClientSession() as session:
        assert await _client(session, dpop_key, fake_server.url).energy_balance("a") == {"balance": "1.00"}


async def test_nonce_retry_on_401(fake_server, dpop_key):
    fake_server.add("GET", "/v1/energy/accounts/a/balance", status=401, body="",
                    headers={"WWW-Authenticate": 'DPoP error="use_dpop_nonce"', "DPoP-Nonce": "n2"})
    fake_server.add("GET", "/v1/energy/accounts/a/balance", json={"data": {}})
    async with aiohttp.ClientSession() as session:
        await _client(session, dpop_key, fake_server.url).energy_balance("a")
    proof = jwt.decode(fake_server.requests[1]["headers"]["DPoP"], options={"verify_signature": False})
    assert proof["nonce"] == "n2"


async def test_pagination_and_dpop_header(fake_server, dpop_key):
    base = fake_server.url
    fake_server.add("GET", "/v1/energy/accounts",
                    json={"data": [{"accountId": "1"}], "links": {"next": f"{base}/v1/energy/accounts?page=2"}})
    fake_server.add("GET", "/v1/energy/accounts", json={"data": [{"accountId": "2"}], "links": {}})
    async with aiohttp.ClientSession() as session:
        items = await _client(session, dpop_key, base).energy_accounts()
    assert [i["accountId"] for i in items] == ["1", "2"]
    first = fake_server.requests[0]
    assert first["query"]["limit"] == "1000"
    assert first["headers"]["Authorization"] == "DPoP at"
    claims = jwt.decode(first["headers"]["DPoP"], options={"verify_signature": False})
    assert claims["htu"] == f"{base}/v1/energy/accounts"
    assert claims["ath"]


async def test_ids_keep_raw_equals(fake_server, dpop_key):
    fake_server.add("GET", "/v1/energy/accounts/LTmp=/balance", json={"data": {}})
    async with aiohttp.ClientSession() as session:
        await _client(session, dpop_key, fake_server.url).energy_balance("LTmp=")
    assert fake_server.requests[0]["raw_path"] == "/v1/energy/accounts/LTmp=/balance"


async def test_der_404_is_none(fake_server, dpop_key):
    fake_server.add("GET", "/v1/energy/accounts/a/meters/m/der", status=404, json={})
    async with aiohttp.ClientSession() as session:
        assert await _client(session, dpop_key, fake_server.url).energy_der("a", "m") is None


async def test_revocation_cancels_siblings():
    cancelled = asyncio.Event()

    async def slow():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def revoked():
        raise api.GrantRevokedError(403, "disclosure_grant_required")

    with pytest.raises(api.GrantRevokedError):
        await api.cancel_on_revocation([slow(), revoked()])
    assert cancelled.is_set()


async def test_other_errors_returned_in_place():
    async def ok():
        return 1

    async def bad():
        raise api.BalanceUnavailableError(403, "balance_unavailable")

    result = await api.cancel_on_revocation([ok(), bad()])
    assert result[0] == 1
    assert isinstance(result[1], api.BalanceUnavailableError)


async def test_next_link_off_origin_refused(fake_server, dpop_key):
    fake_server.add("GET", "/v1/energy/accounts",
                    json={"data": [{"accountId": "1"}], "links": {"next": "https://elsewhere.example/steal"}})
    async with aiohttp.ClientSession() as session:
        with pytest.raises(api.OpenfeedApiError) as err:
            await _client(session, dpop_key, fake_server.url).energy_accounts()
    assert err.value.code == "invalid_next_link"
    assert len(fake_server.requests) == 1


async def test_relative_next_link_followed(fake_server, dpop_key):
    fake_server.add("GET", "/v1/energy/accounts",
                    json={"data": [{"accountId": "1"}], "links": {"next": "/v1/energy/accounts?page=2"}})
    fake_server.add("GET", "/v1/energy/accounts", json={"data": [{"accountId": "2"}]})
    async with aiohttp.ClientSession() as session:
        items = await _client(session, dpop_key, fake_server.url).energy_accounts()
    assert [i["accountId"] for i in items] == ["1", "2"]
    assert fake_server.requests[1]["query"] == {"page": "2"}


async def test_repeating_next_link_stops(fake_server, dpop_key):
    base = fake_server.url
    loop = f"{base}/v1/energy/accounts?page=2"
    fake_server.add("GET", "/v1/energy/accounts", json={"data": [], "links": {"next": loop}},
                    repeat=True)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(api.OpenfeedApiError) as err:
            await _client(session, dpop_key, base).energy_accounts()
    assert err.value.code == "invalid_next_link"
    assert len(fake_server.requests) == 2


def test_same_origin():
    assert api.same_origin("https://api.openfeed.au/v1/x?y=1", "https://api.openfeed.au")
    assert not api.same_origin("http://api.openfeed.au/v1/x", "https://api.openfeed.au")
    assert not api.same_origin("https://api.openfeed.au:8443/v1", "https://api.openfeed.au")
    assert not api.same_origin("https://api.openfeed.au.evil.example/v1", "https://api.openfeed.au")


def test_endpoint_template_hides_ids() -> None:
    url = ("https://api.openfeed.au/v1/energy/accounts/LTmp=/meters/Mtr=/usage"
           "?limit=1000&oldestDate=2026-09-16&newestDate=2026-10-15&offset=1000")
    assert api.endpoint_template(url) == (
        "/v1/energy/accounts/{id}/meters/{id}/usage"
        "?oldestDate=2026-09-16&newestDate=2026-10-15&offset=1000")
    assert api.endpoint_template("https://api.openfeed.au/v1/energy/accounts") == "/v1/energy/accounts"


async def test_usage_trace_records_each_page(fake_server, dpop_key):
    base = fake_server.url
    path = "/v1/energy/accounts/a=/meters/m=/usage"
    fake_server.add("GET", path, json={"data": [{"intervalDate": "2026-11-04"}],
                                       "links": {"next": f"{base}{path}?offset=1000"}},
                    headers={"x-fapi-interaction-id": "echo-1"})
    fake_server.add("GET", path, json={"data": [], "links": {}})
    trace: list = []
    async with aiohttp.ClientSession() as session:
        got = await _client(session, dpop_key, base).energy_usage(
            "a=", "m=", "2026-09-16", "2026-10-15", trace=trace)
    assert len(got) == 1
    assert [t["items"] for t in trace] == [1, 0]
    assert [t["has_next"] for t in trace] == [True, False]
    assert trace[0]["endpoint"].startswith("/v1/energy/accounts/{id}/meters/{id}/usage?oldestDate=2026-09-16")
    assert trace[0]["response_x-fapi-interaction-id"] == "echo-1"
    # The interaction ID we sent is the one recorded.
    sent = [r["headers"]["x-fapi-interaction-id"] for r in fake_server.requests]
    assert [t["interaction_id"] for t in trace] == sent
    assert len(set(sent)) == 2
