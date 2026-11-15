"""End-to-end config flow: JWKS, PAR, sign-in callback, consent, entry."""

from __future__ import annotations

import time
from unittest.mock import patch

import jwt
import pytest

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.openfeed.const import (
    CONF_GRANT_ID,
    CONF_SCOPES,
    CONF_SUBJECT,
    CONF_TOKEN,
    DOMAIN,
    PENDING_KEY,
)
from custom_components.openfeed.crypto import jwk_thumbprint, public_jwk

ISSUER = "https://auth.openfeed.au"
CLIENT_ID = "app-0b7c2f4e-1a2b-4c3d-8e9f-0123456789ab"
APP_ID = "0b7c2f4e-1a2b-4c3d-8e9f-0123456789ab"
DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/auth",
    "token_endpoint": f"{ISSUER}/token",
    "pushed_authorization_request_endpoint": f"{ISSUER}/request",
    "jwks_uri": f"{ISSUER}/jwks",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """The recorder must start before hass, so request it ahead of the integrations."""
    yield


@pytest.fixture
def server_key(app_key):
    """Stand-in for openfeed's id_token signing key (any RSA key will do)."""
    return app_key


def _id_token(server_key, nonce: str) -> str:
    kid = jwk_thumbprint(public_jwk(server_key))
    now = int(time.time())
    return jwt.encode(
        {"iss": ISSUER, "aud": CLIENT_ID, "sub": "user-1", "nonce": nonce, "iat": now, "exp": now + 300},
        server_key, algorithm="PS256", headers={"kid": kid},
    )


@pytest.fixture
def openfeed(aioclient_mock, server_key, key_pems):
    """Mock openfeed. Token responses come from a queue the test fills."""
    state = {"nonce": None, "tokens": []}
    jwk = {**public_jwk(server_key), "kid": jwk_thumbprint(public_jwk(server_key)), "alg": "PS256"}
    aioclient_mock.get(f"{ISSUER}/.well-known/openid-configuration", json=DISCOVERY)
    aioclient_mock.get(f"{ISSUER}/jwks", json={"keys": [jwk]})

    async def par(method, url, data):
        state["nonce"] = data["nonce"]
        state["par"] = data
        return AiohttpClientMockResponse(method, url, status=201, json={"request_uri": "urn:par:1"})

    async def token(method, url, data):
        body = state["tokens"].pop(0)
        if body.get("id_token") == "SIGN":
            body = {**body, "id_token": _id_token(server_key, state["nonce"])}
        return AiohttpClientMockResponse(method, url, json=body)

    aioclient_mock.post(f"{ISSUER}/request", side_effect=par)
    aioclient_mock.post(f"{ISSUER}/token", side_effect=token)
    with patch(
        "custom_components.openfeed.config_flow.generate_private_key_pem",
        side_effect=list(key_pems) * 4,
    ), patch("custom_components.openfeed.async_setup_entry", return_value=True):
        yield state


def _pending_state(hass: HomeAssistant, kind: str) -> str:
    return next(s for s, p in hass.data[PENDING_KEY].items() if p.kind == kind)


async def _start(hass: HomeAssistant, banking: bool = False):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert '"alg": "PS256"' in result["description_placeholders"]["jwks"]
    assert '"d"' not in result["description_placeholders"]["jwks"]
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"app_id": APP_ID, "redirect_base": "https://ha.example",
         "include_banking": banking},
    )


async def test_full_flow_with_consent(hass: HomeAssistant, openfeed, aioclient_mock) -> None:
    openfeed["tokens"] = [
        {"access_token": "a1", "refresh_token": "r1", "expires_in": 600, "id_token": "SIGN"},
        {"access_token": "a2", "expires_in": 600,
         "authorization_details": [{"type": "disclosure_grant_v1", "grant_id": "g-1"}]},
    ]
    result = await _start(hass, banking=True)
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert result["url"] == f"{ISSUER}/auth?client_id={CLIENT_ID}&request_uri=urn%3Apar%3A1"
    assert openfeed["par"]["resource"] == "https://api.openfeed.au"
    assert openfeed["par"]["redirect_uri"] == "https://ha.example/api/openfeed/callback"
    assert "openfeed-au:data:banking:read" in openfeed["par"]["scope"].split()

    state = _pending_state(hass, "auth")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "c1", "iss": ISSUER, "state": state})
    assert result["type"] is FlowResultType.EXTERNAL_STEP_DONE
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert result["url"].startswith("https://app.openfeed.au/grants/disclosure?appId=" + APP_ID)

    consent_state = _pending_state(hass, "consent")
    assert consent_state in result["url"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"grantId": "g-1", "consented": "true"})
    assert result["type"] is FlowResultType.EXTERNAL_STEP_DONE
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_GRANT_ID] == "g-1"
    assert data[CONF_SUBJECT] == "user-1"
    assert data[CONF_TOKEN]["refresh_token"] == "r1"
    assert "openid" in data[CONF_SCOPES]
    assert result["result"].unique_id == f"{CLIENT_ID}_user-1"
    assert not hass.data[PENDING_KEY]


async def test_state_mismatch_returns_to_form(hass: HomeAssistant, openfeed) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "c1", "iss": ISSUER, "state": "forged"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "state_mismatch"}


async def test_issuer_mismatch_returns_to_form(hass: HomeAssistant, openfeed) -> None:
    result = await _start(hass)
    state = _pending_state(hass, "auth")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "c1", "iss": "https://evil.example", "state": state})
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["errors"] == {"base": "issuer_mismatch"}


async def test_consent_declined(hass: HomeAssistant, openfeed) -> None:
    openfeed["tokens"] = [{"access_token": "a1", "refresh_token": "r1", "expires_in": 600, "id_token": "SIGN"}]
    result = await _start(hass)
    state = _pending_state(hass, "auth")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "c1", "iss": ISSUER, "state": state})
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"consented": "false", "reason": "user_cancelled"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["errors"] == {"base": "consent_declined"}


async def test_invalid_ids_show_errors(hass: HomeAssistant, openfeed) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"app_id": "not-an-id", "redirect_base": "ha.local", "include_banking": False},
    )
    assert result["errors"] == {"app_id": "invalid_app_id", "redirect_base": "invalid_url"}


async def test_reauth_replaces_existing_grant(hass: HomeAssistant, openfeed, key_pems) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=f"{CLIENT_ID}_user-1",
        data={"client_id": CLIENT_ID, "app_id": APP_ID, "redirect_base": "https://ha.example",
              "issuer": ISSUER, "scopes": ["openid", "offline_access", "energy"],
              "app_key_pem": key_pems[0], "dpop_key_pem": key_pems[1], "grant_id": "g-1",
              "token": {"access_token": "old", "refresh_token": "old", "expires_at": 0}},
    )
    entry.add_to_hass(hass)
    openfeed["tokens"] = [
        {"access_token": "a3", "refresh_token": "r3", "expires_in": 600, "id_token": "SIGN",
         "authorization_details": [{"type": "disclosure_grant_v1", "grant_id": "g-1"}]},
    ]
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert openfeed["par"]["grant_management_action"] == "replace"
    assert openfeed["par"]["grant_id"] == "g-1"
    state = _pending_state(hass, "auth")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "c3", "iss": ISSUER, "state": state})
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN]["access_token"] == "a3"


async def test_reconfigure_changes_redirect_url(hass: HomeAssistant, openfeed, key_pems) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=f"{CLIENT_ID}_user-1",
        data={"client_id": CLIENT_ID, "app_id": APP_ID, "redirect_base": "https://ha.example",
              "issuer": ISSUER, "scopes": ["openid", "offline_access", "energy"],
              "app_key_pem": key_pems[0], "dpop_key_pem": key_pems[1], "grant_id": "g-1",
              "token": {"access_token": "old", "refresh_token": "old", "expires_at": 0}},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"include_banking": False, "redirect_base": "ftp://nope"})
    assert result["errors"] == {"redirect_base": "invalid_url"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"include_banking": False, "redirect_base": "https://abc123.ui.nabu.casa/"})
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert openfeed["par"]["redirect_uri"] == "https://abc123.ui.nabu.casa/api/openfeed/callback"
    assert openfeed["par"]["grant_management_action"] == "replace"


@pytest.mark.parametrize(
    ("base", "allow", "expected"),
    [
        ("https://ha.example", False, None),
        ("http://localhost:8123", False, None),
        ("http://127.0.0.1:8123", False, None),
        ("http://[::1]:8123", False, None),
        ("http://homeassistant.local:8123", False, "insecure_url"),
        ("http://192.168.1.10:8123", False, "insecure_url"),
        ("http://homeassistant.local:8123", True, None),
        ("ftp://ha.example", True, "invalid_url"),
        ("https://", False, "invalid_url"),
        ("ha.local", False, "invalid_url"),
    ],
)
def test_redirect_error(base, allow, expected) -> None:
    from custom_components.openfeed.config_flow import redirect_error

    assert redirect_error(base, allow) == expected


async def test_plain_http_needs_explicit_opt_in(hass: HomeAssistant, openfeed, caplog) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert "allow_insecure_redirect" not in result["data_schema"].schema
    user = {"app_id": APP_ID, "redirect_base": "http://homeassistant.local:8123",
            "include_banking": False}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user)
    assert result["errors"] == {"redirect_base": "insecure_url"}
    # The opt-in box appears only after a plain http URL was refused.
    assert "allow_insecure_redirect" in result["data_schema"].schema
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**user, "allow_insecure_redirect": True})
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert openfeed["par"]["redirect_uri"] == "http://homeassistant.local:8123/api/openfeed/callback"
    assert "plain http:// URL" in caplog.text


async def test_reconfigure_refuses_plain_http(hass: HomeAssistant, openfeed, key_pems) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=f"{CLIENT_ID}_user-1",
        data={"client_id": CLIENT_ID, "app_id": APP_ID, "redirect_base": "https://ha.example",
              "issuer": ISSUER, "scopes": ["openid", "offline_access", "energy"],
              "app_key_pem": key_pems[0], "dpop_key_pem": key_pems[1], "grant_id": "g-1",
              "token": {"access_token": "old", "refresh_token": "old", "expires_at": 0}},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"include_banking": False, "redirect_base": "http://10.0.0.5:8123"})
    assert result["errors"] == {"redirect_base": "insecure_url"}
    assert "allow_insecure_redirect" in result["data_schema"].schema



def _jwks(result) -> str:
    return result["description_placeholders"]["jwks"]


async def test_retry_after_failure_keeps_keys_and_values(hass: HomeAssistant, openfeed) -> None:
    """A failed sign-in returns to the form with the same JWKS and the values entered."""
    openfeed["tokens"] = [
        {"access_token": "a1", "refresh_token": "r1", "expires_in": 600, "id_token": "SIGN",
         "authorization_details": [{"type": "disclosure_grant_v1", "grant_id": "g-1"}]},
    ]
    first = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await _start(hass)
    flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"error": "access_denied", "state": _pending_state(hass, "auth")})
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["errors"] == {"base": "access_denied"}
    assert _jwks(result) == _jwks(first)
    assert result["description_placeholders"]["kid"] in _jwks(result)
    # The form comes back with what the user typed.
    suggested = {str(k): k.description for k in result["data_schema"].schema}
    assert suggested["app_id"] == {"suggested_value": APP_ID}
    # The second attempt completes with the same keys.
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"app_id": APP_ID, "redirect_base": "https://ha.example",
                  "include_banking": False})
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"code": "c1", "iss": ISSUER, "state": _pending_state(hass, "auth")})
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_new_flow_reuses_unfinished_keys(hass: HomeAssistant, openfeed, hass_storage) -> None:
    """Closing the dialog and starting again keeps the JWKS already pasted into openfeed."""
    first = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    hass.config_entries.flow.async_abort(first["flow_id"])
    second = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert _jwks(second) == _jwks(first)
    assert "openfeed.pending_keys" in hass_storage


async def test_keys_forgotten_after_entry_created(hass: HomeAssistant, openfeed, hass_storage) -> None:
    openfeed["tokens"] = [
        {"access_token": "a1", "refresh_token": "r1", "expires_in": 600, "id_token": "SIGN",
         "authorization_details": [{"type": "disclosure_grant_v1", "grant_id": "g-1"}]},
    ]
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "c1", "iss": ISSUER, "state": _pending_state(hass, "auth")})
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert "openfeed.pending_keys" not in hass_storage


async def test_par_failure_returns_to_form(hass: HomeAssistant, openfeed, aioclient_mock) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{ISSUER}/.well-known/openid-configuration", json=DISCOVERY)
    aioclient_mock.post(f"{ISSUER}/request", status=401, json={"error": "invalid_client"})
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_client"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (APP_ID, APP_ID),
        (CLIENT_ID, APP_ID),
        (APP_ID.upper(), APP_ID),
        (f"  https://app.openfeed.au/registered-apps/{APP_ID}  ", APP_ID),
        ("not-an-id", None),
        ("", None),
        (f"{APP_ID} 11111111-2222-3333-4444-555555555555", None),
    ],
)
def test_parse_app_id(value, expected) -> None:
    from custom_components.openfeed.config_flow import parse_app_id

    assert parse_app_id(value) == expected


async def test_client_id_derived_from_app_id(hass: HomeAssistant, openfeed) -> None:
    """Pasting the client ID (or the page URL) still gives the right IDs."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert set(map(str, result["data_schema"].schema)) == {"app_id", "include_banking", "redirect_base"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"app_id": f"https://app.openfeed.au/registered-apps/{APP_ID}", "redirect_base": "https://ha.example",
         "include_banking": False},
    )
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert result["url"] == f"{ISSUER}/auth?client_id={CLIENT_ID}&request_uri=urn%3Apar%3A1"


async def test_form_shows_hosted_jwks_url(hass: HomeAssistant, openfeed) -> None:
    await hass.config.async_update(external_url="https://ha.example")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["description_placeholders"]["jwks_url"] == "https://ha.example/api/openfeed/jwks.json"


async def test_jwks_view_serves_public_app_keys(
    hass: HomeAssistant, openfeed, key_pems, hass_client_no_auth
) -> None:
    """The hosted JWKS lists the unfinished setup's and each entry's app key, never a private part or DPoP key."""
    from custom_components.openfeed.crypto import load_private_key, public_jwks

    entry_app, entry_dpop = key_pems
    MockConfigEntry(
        domain=DOMAIN, unique_id="x",
        data={"client_id": CLIENT_ID, "app_id": APP_ID, "app_key_pem": entry_app, "dpop_key_pem": entry_dpop},
    ).add_to_hass(hass)
    await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    client = await hass_client_no_auth()
    resp = await client.get("/api/openfeed/jwks.json")
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-store"
    body = await resp.json()
    kids = [k["kid"] for k in body["keys"]]
    assert public_jwks(load_private_key(entry_app))["keys"][0]["kid"] in kids
    assert public_jwks(load_private_key(entry_dpop))["keys"][0]["kid"] not in kids
    assert all(set(k) == {"kty", "n", "e", "kid", "use", "alg"} for k in body["keys"])
    assert len(kids) == len(set(kids))


async def test_options_offer_bank_transactions_only_with_banking(hass: HomeAssistant) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.openfeed.const import DOMAIN

    energy_only = MockConfigEntry(domain=DOMAIN, data={"scopes": ["openfeed-au:data:energy:read"]})
    energy_only.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(energy_only.entry_id)
    assert "bank_transactions" not in {str(k) for k in result["data_schema"].schema}

    banking = MockConfigEntry(domain=DOMAIN, data={"scopes": [
        "openfeed-au:data:energy:read", "openfeed-au:data:banking:read"]})
    banking.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(banking.entry_id)
    assert "bank_transactions" in {str(k) for k in result["data_schema"].schema}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"backfill_days": 60, "sidebar_dashboard": True, "bank_transactions": True})
    assert banking.options["bank_transactions"] is True
