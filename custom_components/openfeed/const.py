"""Constants for the openfeed Consumer Data Right integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "openfeed"

DEFAULT_ISSUER: Final = "https://auth.openfeed.au"
DEFAULT_RESOURCE: Final = "https://api.openfeed.au"
DEFAULT_API_BASE: Final = "https://api.openfeed.au"
CONSENT_URL: Final = "https://app.openfeed.au/grants/disclosure"
DASHBOARD_URL: Final = "https://app.openfeed.au"
# Opens Settings > Dashboards > Energy, where the meter statistics are added.
ENERGY_CONFIG_URL: Final = "homeassistant://config/energy"
REGISTER_URL: Final = "https://app.openfeed.au/registered-apps/new"

SCOPE_OPENID: Final = "openid"
SCOPE_OFFLINE: Final = "offline_access"
SCOPE_ENERGY: Final = "openfeed-au:data:energy:read"
SCOPE_BANKING: Final = "openfeed-au:data:banking:read"

# Config entry data keys.
CONF_CLIENT_ID: Final = "client_id"
CONF_APP_ID: Final = "app_id"
CONF_ISSUER: Final = "issuer"
CONF_REDIRECT_BASE: Final = "redirect_base"
CONF_INCLUDE_BANKING: Final = "include_banking"
# Flow-only: the user accepts a plain http:// redirect to a non-loopback host.
CONF_ALLOW_HTTP: Final = "allow_insecure_redirect"
CONF_SCOPES: Final = "scopes"
CONF_APP_KEY: Final = "app_key_pem"
CONF_DPOP_KEY: Final = "dpop_key_pem"
CONF_TOKEN: Final = "token"
CONF_GRANT_ID: Final = "grant_id"
CONF_SUBJECT: Final = "subject"
# How meters are keyed in statistic IDs, unique IDs and device identifiers.
# "legacy" (entries made before minor version 2) keeps nmi_<nmi> so existing
# Energy dashboard sources and history keep working. Newer entries key each
# meter by an HMAC of the NMI with a random per-entry salt.
CONF_METER_KEY_SCHEME: Final = "meter_key_scheme"
CONF_METER_KEY_SALT: Final = "meter_key_salt"
METER_KEY_LEGACY: Final = "legacy"
# Bumped when a money statistic changes meaning and must be rebuilt.
CONF_MONEY_STATS_VERSION: Final = "money_stats_version"
# 3: v0.1.5 rebuilds payments again; v0.1.4 could carry a stale running sum.
MONEY_STATS_VERSION: Final = 3

# Options.
CONF_SIDEBAR_DASHBOARD: Final = "sidebar_dashboard"
DASHBOARD_URL_PATH: Final = "openfeed"
CONF_BACKFILL_DAYS: Final = "backfill_days"
# The CDR makes the last 2 years of usage required consumer data.
DEFAULT_BACKFILL_DAYS: Final = 730
MAX_BACKFILL_DAYS: Final = 730
# Opt in: fetch bank transactions and keep monthly and 30-day totals only.
CONF_BANK_TRANSACTIONS: Final = "bank_transactions"

# The callback views Home Assistant serves for the browser redirects.
CALLBACK_PATH: Final = "/api/openfeed/callback"
CONSENT_PATH: Final = "/api/openfeed/consent"
# Public app keys, for openfeed's "Hosted JWKS URL" option. Never the DPoP key.
JWKS_PATH: Final = "/api/openfeed/jwks.json"

# Polling. Energy data is batch refreshed by openfeed every 6 hours and
# balances are cached for about 15 minutes, so hourly polling is enough to
# pick up new data promptly without hammering the API.
UPDATE_INTERVAL_MINUTES: Final = 60
USAGE_REFRESH_HOURS: Final = 3
# Re-import this many days on each usage refresh so substituted reads that
# are later finalised overwrite the earlier values.
USAGE_RESTATE_DAYS: Final = 7
# The usage endpoint may return 500 for windows longer than 30 days.
USAGE_WINDOW_DAYS: Final = 30

# Account detail (plans), invoices and billing change slowly.
ACCOUNT_DETAIL_REFRESH_HOURS: Final = 24
BILLING_REFRESH_HOURS: Final = 6
# openfeed batch-refreshes banking data every 4 hours.
BANK_TRANSACTIONS_REFRESH_HOURS: Final = 4

PENDING_KEY: Final = f"{DOMAIN}_pending"
# Keys generated for a setup that has not finished yet, kept so a retry
# reuses the JWKS already pasted into openfeed. Removed once an entry exists.
PENDING_KEYS_STORAGE: Final = f"{DOMAIN}.pending_keys"
