"""Config flow for openfeed.

Setup, in order:

1. user       The integration generates both PS256 keypairs and publishes
              the app public key at JWKS_PATH. The user registers an app in
              the openfeed dashboard with that Hosted JWKS URL (or pastes the
              JWKS inline) and enters the App ID. The OAuth2 client ID is
              always the App ID with an app- prefix, so it is derived.
2. authorize  External step: a pushed authorisation request, then the
              openfeed sign-in. The callback view validates state and iss,
              and the flow exchanges the code with a DPoP proof.
3. consent    External step, only when no disclosure grant exists yet: the
              openfeed consent deep link where the user picks accounts.
              The flow then refreshes to obtain a grant-bound token.
4. finish     Creates (or, for reauth and reconfigure, updates) the entry.

Private keys are generated inside Home Assistant and never displayed.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from typing import Any
from urllib.parse import urlencode, urlsplit

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store

from .const import (
    CALLBACK_PATH,
    CONF_ALLOW_HTTP,
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_BACKFILL_DAYS,
    CONF_SIDEBAR_DASHBOARD,
    CONF_CLIENT_ID,
    CONF_DPOP_KEY,
    CONF_GRANT_ID,
    CONF_BANK_TRANSACTIONS,
    CONF_INCLUDE_BANKING,
    CONF_ISSUER,
    CONF_REDIRECT_BASE,
    CONF_SCOPES,
    CONF_SUBJECT,
    CONF_TOKEN,
    CONSENT_PATH,
    CONSENT_URL,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_ISSUER,
    DOMAIN,
    JWKS_PATH,
    MAX_BACKFILL_DAYS,
    PENDING_KEYS_STORAGE,
    REGISTER_URL,
    SCOPE_BANKING,
    SCOPE_ENERGY,
    SCOPE_OFFLINE,
    SCOPE_OPENID,
)
from .crypto import (
    generate_private_key_pem,
    jwk_thumbprint,
    load_private_key,
    pkce_pair,
    public_jwk,
    public_jwks,
    random_token,
)
from .oauth import OpenfeedAuthError, TokenSet
from .session import build_auth
from .views import async_register_views, discard_pending, register_pending

_LOGGER = logging.getLogger(__name__)

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def parse_app_id(value: str) -> str | None:
    """The App ID from whatever the user pasted.

    Accepts the bare App ID, the app- prefixed OAuth2 client ID, or the app's
    page URL in the openfeed dashboard. Returns the lower-case UUID, or None.
    """
    found = _UUID_RE.findall(value or "")
    return found[-1].lower() if len(set(f.lower() for f in found)) == 1 else None


def client_id_for(app_id: str) -> str:
    """openfeed's OAuth2 client ID is the App ID with an app- prefix."""
    return f"app-{app_id}"


def jwks_url(base: str) -> str:
    return f"{base.rstrip('/')}{JWKS_PATH}" if base else ""


# Failures that leave the generated keys valid: the user step is shown again
# with the error, so the next attempt reuses the JWKS already in openfeed.
RETRYABLE = frozenset(
    {
        "invalid_client",
        "invalid_scope",
        "par_failed",
        "access_denied",
        "auth_error",
        "state_mismatch",
        "issuer_mismatch",
        "no_refresh_token",
        "token_failed",
        "consent_declined",
        "grant_mismatch",
    }
)


def _key_store(hass: HomeAssistant) -> Store[dict[str, str]]:
    return Store(hass, 1, PENDING_KEYS_STORAGE, private=True)


async def _async_setup_keys(hass: HomeAssistant) -> dict[str, str]:
    """The keypairs for a new setup: the unfinished setup's, else fresh ones."""
    store = _key_store(hass)
    saved = await store.async_load()
    if saved and saved.get(CONF_APP_KEY) and saved.get(CONF_DPOP_KEY):
        return {CONF_APP_KEY: saved[CONF_APP_KEY], CONF_DPOP_KEY: saved[CONF_DPOP_KEY]}
    app_pem, dpop_pem = await hass.async_add_executor_job(
        lambda: (generate_private_key_pem(), generate_private_key_pem())
    )
    keys = {CONF_APP_KEY: app_pem, CONF_DPOP_KEY: dpop_pem}
    await store.async_save(keys)
    return keys


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_insecure_redirect(base: str) -> bool:
    """True for a plain http:// URL to anything but a loopback host."""
    parts = urlsplit(base)
    return parts.scheme == "http" and not _is_loopback(parts.hostname or "")


def redirect_error(base: str, allow_http: bool) -> str | None:
    """Validate the Home Assistant URL openfeed returns the browser to.

    FAPI 2.0 requires https redirect URIs, except for loopback. The code and
    the consent return travel in the URL, so plain http to another host is
    only accepted when the user explicitly allows it.
    """
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "invalid_url"
    if is_insecure_redirect(base) and not allow_http:
        return "insecure_url"
    return None


def _scopes(include_banking: bool) -> list[str]:
    scopes = [SCOPE_OPENID, SCOPE_OFFLINE, SCOPE_ENERGY]
    if include_banking:
        scopes.append(SCOPE_BANKING)
    return scopes


class OpenfeedConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle an openfeed config flow."""

    VERSION = 1
    # 2: meters are keyed by a salted hash, not the NMI (see async_migrate_entry).
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._verifier: str | None = None
        self._state: str | None = None
        self._nonce: str | None = None
        self._redirect_uri: str | None = None
        self._tokens: TokenSet | None = None
        self._subject: str | None = None
        self._failure: str | None = None
        self._grant_action = "create"
        # Show the "allow plain http" box once a plain http URL was refused.
        self._offer_http = False
        # A sign-in failure to show on the user step, and its detail.
        self._retry_error: str | None = None
        self._retry_detail = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OpenfeedOptionsFlow()

    # Step 1 ----------------------------------------------------------------

    def _default_base(self) -> str:
        try:
            return get_url(self.hass, allow_ip=True, prefer_external=True)
        except NoURLAvailableError:
            return ""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the JWKS and collect the registered app's IDs."""
        async_register_views(self.hass)
        if CONF_APP_KEY not in self._data:
            self._data.update(await _async_setup_keys(self.hass))

        errors: dict[str, str] = {}
        if self._retry_error:
            errors["base"] = self._retry_error
            self._retry_error = None
        elif user_input is not None:
            app_id = parse_app_id(user_input[CONF_APP_ID])
            base = str(user_input.get(CONF_REDIRECT_BASE) or self._default_base()).strip().rstrip("/")
            if not app_id:
                errors[CONF_APP_ID] = "invalid_app_id"
            if error := redirect_error(base, bool(user_input.get(CONF_ALLOW_HTTP))):
                errors[CONF_REDIRECT_BASE] = error
                self._offer_http = self._offer_http or error == "insecure_url"
            if not errors:
                assert app_id is not None
                self._data.update(
                    {
                        CONF_CLIENT_ID: client_id_for(app_id),
                        CONF_APP_ID: app_id,
                        CONF_REDIRECT_BASE: base,
                        CONF_ISSUER: DEFAULT_ISSUER,
                        CONF_SCOPES: _scopes(user_input[CONF_INCLUDE_BANKING]),
                    }
                )
                return await self.async_step_authorize()

        app_key = load_private_key(self._data[CONF_APP_KEY])
        jwks = public_jwks(app_key)
        # Prefill what the user entered last time when returning after a failure.
        if user_input is None and CONF_APP_ID in self._data:
            user_input = {
                CONF_APP_ID: self._data[CONF_APP_ID],
                CONF_REDIRECT_BASE: self._data[CONF_REDIRECT_BASE],
                CONF_INCLUDE_BANKING: SCOPE_BANKING in self._data.get(CONF_SCOPES, []),
            }
        default_base = self._default_base()
        fields: dict[Any, Any] = {
            vol.Required(CONF_APP_ID): str,
            vol.Required(CONF_INCLUDE_BANKING, default=False): bool,
            vol.Required(CONF_REDIRECT_BASE, default=default_base): str,
        }
        if self._offer_http:
            fields[vol.Optional(CONF_ALLOW_HTTP, default=False)] = bool
        schema = vol.Schema(fields)
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        base_for_url = str((user_input or {}).get(CONF_REDIRECT_BASE) or default_base).rstrip("/")
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            description_placeholders={
                "register_url": REGISTER_URL,
                "jwks_url": jwks_url(base_for_url) or f"<your Home Assistant URL>{JWKS_PATH}",
                "jwks": json.dumps(jwks),
                "kid": jwk_thumbprint(public_jwk(app_key)),
                "error": self._retry_detail,
            },
            errors=errors,
        )

    # Step 2 ----------------------------------------------------------------

    async def _start_authorization(self) -> str:
        auth = build_auth(self.hass, self._data)
        self._verifier, challenge = pkce_pair()
        self._state = random_token()
        self._nonce = random_token()
        self._redirect_uri = f"{self._data[CONF_REDIRECT_BASE]}{CALLBACK_PATH}"
        if is_insecure_redirect(self._data[CONF_REDIRECT_BASE]):
            _LOGGER.warning(
                "openfeed sign-in returns to a plain http:// URL; the authorisation code "
                "travels unencrypted on your network. Use an https URL if you can"
            )
        grant_id = self._data.get(CONF_GRANT_ID) if self._grant_action == "replace" else None
        request_uri = await auth.push_authorization_request(
            redirect_uri=self._redirect_uri,
            scopes=self._data[CONF_SCOPES],
            state=self._state,
            nonce=self._nonce,
            code_challenge=challenge,
            grant_management_action="replace" if grant_id else "create",
            grant_id=grant_id,
        )
        register_pending(self.hass, self._state, self.flow_id, "auth")
        return auth.authorize_url(request_uri)

    async def async_step_authorize(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Run the sign-in as an external step."""
        if user_input is None:
            try:
                url = await self._start_authorization()
            except OpenfeedAuthError as err:
                _LOGGER.warning("openfeed pushed authorisation request failed: %s", err)
                reason = "invalid_client" if err.error == "invalid_client" else "par_failed"
                if err.error == "invalid_scope":
                    reason = "invalid_scope"
                if self.source == SOURCE_USER:
                    return await self._async_retry(reason, str(err))
                return self.async_abort(reason=reason, description_placeholders={"error": str(err)})
            return self.async_external_step(step_id="authorize", url=url)

        self._failure = await self._handle_callback(user_input)
        if self._failure:
            return self.async_external_step_done(next_step_id="failed")
        assert self._tokens is not None
        if self._tokens.grant_id:
            return self.async_external_step_done(next_step_id="finish")
        return self.async_external_step_done(next_step_id="consent")

    async def _handle_callback(self, query: dict[str, Any]) -> str | None:
        """Validate the redirect and exchange the code. Returns a failure reason."""
        discard_pending(self.hass, self.flow_id)
        if "error" in query:
            _LOGGER.warning("openfeed sign-in returned %s: %s", query["error"], query.get("error_description"))
            return "access_denied" if query["error"] == "access_denied" else "auth_error"
        if query.get("state") != self._state:
            return "state_mismatch"
        auth = build_auth(self.hass, self._data)
        disc = await auth.discover()
        # RFC 9207: the iss parameter must match the issuer we started with.
        if query.get("iss", "").rstrip("/") != disc.issuer.rstrip("/"):
            return "issuer_mismatch"
        try:
            tokens = await auth.exchange_code(query["code"], self._verifier or "", self._redirect_uri or "")
            if not tokens.refresh_token:
                return "no_refresh_token"
            if tokens.id_token:
                claims = await auth.verify_id_token(tokens.id_token, self._nonce or "")
                self._subject = claims.get("sub")
        except (OpenfeedAuthError, KeyError) as err:
            _LOGGER.warning("openfeed code exchange failed: %s", err)
            return "token_failed"
        self._tokens = tokens
        return None

    # Step 3 ----------------------------------------------------------------

    async def async_step_consent(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Send the user to pick the accounts to share."""
        if user_input is None:
            state = random_token()
            register_pending(self.hass, state, self.flow_id, "consent")
            return_uri = f"{self._data[CONF_REDIRECT_BASE]}{CONSENT_PATH}/{state}"
            url = f"{CONSENT_URL}?{urlencode({'appId': self._data[CONF_APP_ID], 'redirectUri': return_uri})}"
            return self.async_external_step(step_id="consent", url=url)

        discard_pending(self.hass, self.flow_id)
        if user_input.get("consented") != "true":
            _LOGGER.info("openfeed consent declined: %s", user_input.get("reason"))
            self._failure = "consent_declined"
            return self.async_external_step_done(next_step_id="failed")
        # The new access token carries the grant without a second sign-in.
        assert self._tokens is not None
        try:
            refreshed = await build_auth(self.hass, self._data).refresh(self._tokens)
        except OpenfeedAuthError as err:
            _LOGGER.warning("openfeed post-consent refresh failed: %s", err)
            self._failure = "token_failed"
            return self.async_external_step_done(next_step_id="failed")
        expected = user_input.get("grantId")
        if not refreshed.grant_id or (expected and refreshed.grant_id != expected):
            self._failure = "grant_mismatch"
            return self.async_external_step_done(next_step_id="failed")
        self._tokens = refreshed
        return self.async_external_step_done(next_step_id="finish")

    # Step 4 ----------------------------------------------------------------

    async def async_step_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        reason = self._failure or "auth_error"
        if self.source == SOURCE_USER and reason in RETRYABLE:
            return await self._async_retry(reason)
        return self.async_abort(reason=reason)

    async def _async_retry(self, reason: str, detail: str = "") -> ConfigFlowResult:
        """Show the user step again with the error, keeping the same keys."""
        self._retry_error = reason
        self._retry_detail = detail
        self._tokens = None
        self._subject = None
        return await self.async_step_user()

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Create or update the entry."""
        assert self._tokens is not None
        data = {
            **self._data,
            CONF_TOKEN: self._tokens.as_dict(),
            CONF_GRANT_ID: self._tokens.grant_id,
        }
        if self._subject:
            data[CONF_SUBJECT] = self._subject
        unique_id = f"{data[CONF_CLIENT_ID]}_{self._subject or 'user'}"

        if self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
            entry = (
                self._get_reauth_entry() if self.source == SOURCE_REAUTH else self._get_reconfigure_entry()
            )
            if entry.unique_id and entry.unique_id != unique_id:
                return self.async_abort(reason="wrong_account")
            return self.async_update_reload_and_abort(entry, data=data)

        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        # The keys now live in the entry; the next setup starts with new ones.
        await _key_store(self.hass).async_remove()
        return self.async_create_entry(title="openfeed CDR", data=data)

    # Reauth and reconfigure --------------------------------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Sign in again, keeping the keys and registered app."""
        self._data = dict(entry_data)
        self._grant_action = "replace" if entry_data.get(CONF_GRANT_ID) else "create"
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        async_register_views(self.hass)
        return await self.async_step_authorize()

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change which accounts are shared (grant replace) or the redirect URL."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            base = str(user_input.get(CONF_REDIRECT_BASE) or "").strip().rstrip("/")
            if error := redirect_error(base, bool(user_input.get(CONF_ALLOW_HTTP))):
                errors[CONF_REDIRECT_BASE] = error
                self._offer_http = self._offer_http or error == "insecure_url"
            else:
                self._data = {
                    **entry.data,
                    CONF_SCOPES: _scopes(user_input[CONF_INCLUDE_BANKING]),
                    CONF_REDIRECT_BASE: base,
                }
                self._grant_action = "replace" if entry.data.get(CONF_GRANT_ID) else "create"
                async_register_views(self.hass)
                return await self.async_step_authorize()
        fields: dict[Any, Any] = {
            vol.Required(
                CONF_INCLUDE_BANKING,
                default=SCOPE_BANKING in entry.data.get(CONF_SCOPES, []),
            ): bool,
            vol.Required(
                CONF_REDIRECT_BASE,
                default=entry.data.get(CONF_REDIRECT_BASE) or self._default_base(),
            ): str,
        }
        if self._offer_http:
            fields[vol.Optional(CONF_ALLOW_HTTP, default=False)] = bool
        schema = vol.Schema(fields)
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)


class OpenfeedOptionsFlow(OptionsFlow):
    """Options: usage backfill depth and the sidebar dashboard."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
        sidebar = self.config_entry.options.get(CONF_SIDEBAR_DASHBOARD, True)
        fields: dict[Any, Any] = {
            vol.Required(CONF_BACKFILL_DAYS, default=current): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=MAX_BACKFILL_DAYS)
            ),
            vol.Required(CONF_SIDEBAR_DASHBOARD, default=sidebar): bool,
        }
        # Transaction totals only make sense when banking is shared.
        if SCOPE_BANKING in self.config_entry.data.get(CONF_SCOPES, []):
            fields[
                vol.Required(
                    CONF_BANK_TRANSACTIONS,
                    default=self.config_entry.options.get(CONF_BANK_TRANSACTIONS, False),
                )
            ] = bool
        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields))
