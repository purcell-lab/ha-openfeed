# openfeed Consumer Data Right for Home Assistant

Bring your own Consumer Data Right (CDR) energy data into Home Assistant through
[openfeed](https://openfeed.au). You get your retailer's half-hourly meter reads in the
Energy dashboard, your account balance and latest bill as sensors, and optionally your
bank balances.

![openfeed sidebar dashboard](docs/images/dashboard-desktop.png)

| Version | Home Assistant | Status |
|---|---|---|
| 0.1.7 | 2026.4 or later | Current: plan tariff sensors, meter detail sensors |
| 0.1.0 | 2026.4 or later | First release |

## How it works

The [Consumer Data Right](https://www.cdr.gov.au/) lets you direct a data holder (your
energy retailer or bank) to share your data with an accredited data recipient. Only
ACCC-accredited providers can offer CDR services.

openfeed is the accredited recipient. You connect your retailer and bank to openfeed once.
This integration then receives your data from openfeed by onward disclosure, under a
sharing grant you approve and can revoke at any time from the openfeed dashboard. The
integration never talks to your retailer or bank directly.

Two consents are involved:

1. **openfeed sign-in.** You sign in to openfeed and approve this Home Assistant app.
2. **Disclosure grant.** You choose which accounts openfeed shares with the app.

Revoking the grant in openfeed stops data immediately. The integration discards cached
data and asks you to reconnect.

## What you get

| Data | Entity or statistic | Freshness |
|---|---|---|
| Grid consumption, export, controlled load | External statistics `openfeed:<meter_key>_consumption`, `_export`, `_controlled_load` (hourly, kWh) | Batch; openfeed refreshes every 6 hours, meter data usually lags a day |
| Grid import and export power | External statistics `openfeed:<meter_key>_consumption_power`, `_export_power` (hourly mean, min and max, kW, from the meter's 5 or 30 minute reads) | As above |
| Latest full day per series | `sensor.<meter>_latest_day_grid_*` | As above |
| Latest full day peak import and export | `sensor.<meter>_latest_day_peak_grid_import`, `_export` (kW, with the interval start) | As above |
| Account balance | `sensor.energy_<account>_account_balance` | Live, cached up to 15 minutes |
| Latest invoice amount, due date, status | `sensor.energy_<account>_latest_invoice_*` | Hourly poll |
| Plan name, fees, discounts and incentives | `sensor.energy_<account>_plan` (only when the retailer shares a plan) | Daily |
| Current import rate | `sensor.energy_<account>_import_rate` (AUD/kWh including GST; rate excluding GST, stepped blocks and network/retail components as attributes) | Re-evaluated every minute from the plan |
| Time-of-use period | `sensor.energy_<account>_tariff_period` (`single_rate`, `peak`, `off_peak`, `shoulder`, `shoulder1`, `shoulder2`) | Every minute |
| Feed-in rate | `sensor.energy_<account>_feed_in_rate` (AUD/kWh as published, retailer and government schemes added) | Every minute |
| Daily supply charge | `sensor.energy_<account>_daily_supply_charge` (AUD/day including GST) | Every minute |
| Demand charge | `sensor.energy_<account>_demand_charge` (including GST, only on demand tariffs) | Every minute |
| Next tariff change | `sensor.energy_<account>_next_tariff_change` (timestamp, looks two days ahead) | Every minute |
| Meter detail | `sensor.<meter>_meter_status`, `_meter_read_type`, `_jurisdiction`, `_service_point_classification`, `_generator`, and `_nmi` (disabled by default). Every meter detail field except the NMI is also an attribute of the status sensor | Daily |
| Month-to-date charges and credits | `sensor.energy_<account>_month_to_date_charges`, `_credits`, with a split by transaction type (only when the retailer shares billing transactions) | Every 6 hours |
| Billing and invoice history | External statistics `openfeed:acct_<hash>_charges` (gross charges), `_credits` (credits), `_payments` (money you paid), `_refunds` (money paid back to you), `_invoiced` (AUD). All but invoiced are positive amounts | Every 6 hours |
| Last payment and last refund | `sensor.<account>_last_payment`, `_last_refund` (amount, with date and method). The refund sensor only appears once a refund has been paid | Every 6 hours |
| Sidebar dashboard | **Energy data** in the sidebar, at `/openfeed` | Live |
| DER approved capacity and export limit | `sensor.<meter>_der_*` (when the retailer publishes DER data) | Hourly poll |
| DER equipment and solar capacity | `sensor.<meter>_der_solar_capacity`, with every inverter and device as attributes | Hourly poll |
| DER register issues | `sensor.<meter>_der_register_issues` | Hourly poll |
| Month-to-date and last 30 days grid import and export | `sensor.<meter>_month_to_date_grid_*`, `_last_30_days_grid_*` (kWh, full days only) | With usage |
| Last statement charges and credits | `sensor.<account>_last_statement_charges`, `_credits` (with net and the statistic IDs as attributes) | Every 6 hours |
| Bills calendar | `calendar.<account>_bills`: one event per invoice, from issue date to due date, with amount and status | Hourly poll |
| Usage history held | `sensor.<meter>_usage_history` (days, with oldest date and every statistic ID) | With usage |
| Substituted intervals | `sensor.<meter>_substituted_intervals` | With usage |
| Bank current and available balance (optional) | `sensor.bank_<account>_*_balance` | Live, cached up to 15 minutes |
| Bank credit limit (cards and loans that report one) | `sensor.bank_<account>_credit_limit` | Live, with the balance |
| Bank interest rate | `sensor.bank_<account>_interest_rate` (%, base rate; every rate and type as attributes) | Daily |
| Bank money in and out (opt in) | `sensor.bank_<account>_month_to_date_money_in`, `_money_out`, `_last_30_days_money_in`, `_money_out` | Every 4 hours |
| Bank interest and fees (opt in) | `sensor.bank_<account>_month_to_date_interest` (net), `_month_to_date_fees` | Every 4 hours |

Add the three external statistics in **Settings > Dashboards > Energy** under grid
consumption and return to grid. The first load imports 60 days of history (change it in
the integration options, up to 365). After that the last 7 days are re-imported every
3 hours so late and substituted reads are corrected.

Basic (accumulation) meter reads are not imported to statistics; only interval reads are.

### Finding the data

**Meter keys and the NMI.** The NMI identifies your premises, so it is kept out of
statistic IDs, entity IDs, device names and attributes. `<meter_key>` is
`meter_<hash>`, an HMAC of the NMI with a random salt kept in the config entry. Meter
devices are named `Meter ending <last 4 digits>`, so `<meter>` is
`meter_ending_<last 4>`. The full NMI is only on the NMI sensor, which is disabled by
default. Entries created with v0.1.7 or earlier keep their existing names and IDs
(`<meter_key>` is `nmi_<nmi>` and `<meter>` is `meter_<nmi>`), so Energy dashboard
sources, history and automations keep working; the NMI attribute on the meter status
sensor is removed from them too.

Most of the history lives in long-term statistics, which have no entity. Three places
surface it:

- **Sensors and calendar.** The summary sensors and bills calendar above answer the
  common questions directly, and each lists the statistic IDs it relates to.
- **Device page.** The meter device's **Visit** link opens the Energy settings, where
  you can add the import and export statistics as a grid source. Only do this if no
  other grid meter feeds your Energy dashboard, or energy is counted twice.
- **Sidebar dashboard.** See below. If you would rather build your own,
  [docs/dashboard.yaml](docs/dashboard.yaml) is the same layout as a view you can
  paste into any dashboard.

### Sidebar dashboard

The integration adds **Energy data** to the sidebar. It is generated from your shared
accounts and meters each time it opens, so it needs no setup and updates with each
release. It is read-only; to customise it, copy the cards from
[docs/dashboard.yaml](docs/dashboard.yaml) into your own dashboard. Turn it off in
**Settings > Devices & services > openfeed > Configure**.

| Section | What it shows |
|---|---|
| Grid energy | Import and export this month, last 30 days and latest day, with latest-day peaks |
| Daily grid energy | 30 days of daily import and export from the meter |
| Grid power | 7 days of hourly mean and peak import and export power |
| Daily peak grid power | 30 days of the highest 5-minute import and export |
| Solar and battery register | DER capacity, export limit, register issues and history depth |
| Bills and payments | Balance, latest invoice, last payment and last refund |
| Charges and credits | This month and last statement |
| Monthly charges, credits and payments | 12 months of each as bars |
| Bills | Invoices on a calendar |

![Grid energy and power](docs/images/dashboard-energy.png)

![Bills, payments and DER register](docs/images/dashboard-billing.png)

Grid power is a day behind, because it comes from your retailer's meter reads. It is
held as long-term statistics, not as an entity, so it does not appear in the History
panel. Use the dashboard or **Developer tools > Statistics** (search "grid import
power").

### How much history

openfeed imports some history when you first connect a retailer, then adds new data
every 6 hours. The `usage_last_fetched` sensor shows the oldest and latest usage dates
openfeed returned, and diagnostics show the oldest invoice and billing transaction.
Home Assistant keeps long-term statistics permanently, so history builds up from the
day you connect even if openfeed only holds a rolling window.

To see exactly what was asked for and returned, download the integration diagnostics.
Each meter's `usage_requests` lists the last few usage runs: the date range requested,
each 30-day window, the records and interval dates returned, and every HTTP page with
its status, time and `x-fapi-interaction-id`. Account and meter IDs are replaced with
`{id}`. Quote the interaction IDs when you ask openfeed support about missing history.
Backfill runs also include `samples`: how many returned days carry readings, plus the raw
record for the newest day with readings and the newest day without, with IDs removed.

Home Assistant long-term statistics are hourly. The energy statistics are hourly sums;
the power statistics keep the hourly maximum of the meter's own interval reads, so a
5-minute peak is not lost.

### Billing

Retailers usually date every billing line on the statement date, so the money
statistics step once a month rather than daily.

Payments and refunds are kept apart. The CDR standard defines a payment amount only as
"the amount paid", and retailers use the account ledger convention where a credit to
your account is negative. So a negative payment line is a payment you made, and a
positive payment line is money paid back to you, such as a refund of a credit balance.
Both are stored as positive amounts in their own series.

Feed-in credits only appear if your retailer itemises them as billing lines. Some
retailers net them into the invoice instead, so the invoice can be well below the
charges.

Diagnostics list every billing line (date, type, amount, GST, payment method) without
descriptions, invoice numbers or meter IDs, so you can check how your retailer reports
credits and refunds.

### Plan tariff

When the retailer shares a plan with tariff detail, the integration works out which
rate applies now from the plan's seasonal periods, time-of-use windows, time zone
(`LOCAL` or `AEST`) and feed-in tariffs. CDR unit prices and supply charges exclude
GST, so the sensors add 10% and keep the published figure as `rate_ex_gst` or
`charge_ex_gst`. Feed-in rates are shown as published. Windows that only apply on
public holidays are ignored, because there is no holiday calendar.

The tariff sensors only appear when the plan carries tariff detail. Some retailers
share no plan: Amber, for example, returns none, because its prices follow the
wholesale market. The diagnostics show `tariff_detail_shared`.

### Meter detail

The meter sensors come from openfeed's meter detail endpoint, read once a day: status,
read type (interval or daily), jurisdiction, service point classification and whether
the service point is registered as a generator. If that endpoint fails, they fall back
to the meters list and the status sensor's `detail_source` attribute shows `list`.
Generator shows unknown when the retailer does not report it.

### DER register

The DER data comes from AEMO's DER Register via your retailer. Your network (DNSP)
maintains it from the connection applications your installer lodges, so it can drift
from what is on site. The DER register issues sensor flags:

| Issue | Meaning |
|---|---|
| `decommissioned_records_listed` | Removed equipment is still on the record |
| `battery_inverter_without_storage_device` | A battery inverter is listed with no storage device |
| `solar_panels_on_battery_inverter` | Solar panels are attached to a battery inverter |
| `active_inverters_exceed_approved_capacity` | Active inverter capacity is above the approved capacity |
| `no_active_equipment` | Nothing on the record is active |

To correct the record, ask your installer to lodge an alteration with your network, or
contact the network directly. Your retailer cannot edit it.

## Scopes

| Scope | Why |
|---|---|
| `openid` | Identifies your openfeed user so one Home Assistant entry maps to one person |
| `offline_access` | Refresh tokens, so Home Assistant stays connected without signing in again |
| `openfeed-au:data:energy:read` | Accounts, plans, balances, billing, invoices, meters, usage and DER |
| `openfeed-au:data:banking:read` | Only when you tick "Also share banking data" |

Payment schedules and personal details are not requested.

**Banking data.** With banking shared, each bank account gets its balances, credit limit
(where the bank reports one) and headline interest rate. The account detail call returns
the unmasked BSB and account number; the integration drops both on arrival, so they never
reach an entity, the log or diagnostics.

Transactions are off by default. Turn on **Summarise bank transactions** in the
integration options to add money in, money out, interest and fee totals for this month
and the last 30 days. Only totals are kept in memory. Descriptions, payees, merchants and
references are never stored, and no transaction statistics are written to the recorder.
Pending transactions are counted but not summed, because they can still change.

## Setup

1. Install the integration (see below). A first Marketplace install needs no restart.
2. Go to **Settings > Devices & services > Add integration > openfeed Consumer Data Right**.
3. In a new tab, [register an app on openfeed](https://app.openfeed.au/registered-apps/new):
   - Scopes: energy, and banking if you want it.
   - Keys: **Hosted JWKS URL**. Enter the URL the setup screen shows,
     `<Home Assistant URL>/api/openfeed/jwks.json`.
4. Paste the **App ID** into Home Assistant. The `app-` client ID or the app's page URL
   also works; the client ID is derived from the App ID.
5. Leave the Home Assistant URL as filled in, unless you sign in from somewhere it does
   not reach. See [Nabu Casa and remote access](#nabu-casa-and-remote-access).
6. Sign in to openfeed, approve the app, then choose the accounts to share.

To change which accounts are shared, or the Home Assistant URL, use **Reconfigure** on
the integration. The existing grant is updated in place rather than a new one created.

## Nabu Casa and remote access

The integration works with Home Assistant Cloud (Nabu Casa), your own domain, or a
local address only.

- **Data is polled.** Home Assistant polls `api.openfeed.au`. There is no webhook, so you
  do not need Nabu Casa webhooks or port forwarding for data to flow. The only inbound
  request is openfeed fetching the public key (see **Hosted public key** below).
- **Sign-in is a browser redirect.** During setup, reauth and reconfigure, openfeed sends
  your browser back to `<Home Assistant URL>/api/openfeed/callback` and
  `/api/openfeed/consent/<state>`. These redirects only have to work in the browser you use.
- **Hosted public key.** Home Assistant serves the app's public key at
  `/api/openfeed/jwks.json`, and openfeed fetches it on each sign-in request. Only the
  public half of the app key is published; the private keys and the DPoP key never
  leave Home Assistant. Re-adding the integration or a new key needs no change in
  openfeed. This URL has to be reachable from the internet (Nabu Casa or your own
  domain). Without one, use the **No public URL?** option on the setup screen and paste the JWKS
  inline instead; you then paste it again whenever the integration is re-added.
- **No redirect URI to register.** openfeed takes the redirect URI from each signed
  pushed authorisation request, so the app registration has no redirect field and any
  URL works.
- **Default URL.** The setup form fills in your external URL, or your Nabu Casa remote
  URL (`https://<id>.ui.nabu.casa`) when no external URL is set, or else your internal
  URL.

| You sign in from | Use this Home Assistant URL |
|---|---|
| Anywhere, with Nabu Casa | Your Nabu Casa remote URL, `https://<id>.ui.nabu.casa` |
| Anywhere, with your own domain | Your domain, for example `https://ha.example.com` |
| A browser on your home network | An https local URL if you have one. A plain `http://homeassistant.local:8123` URL needs **Allow a plain http:// URL** ticked (see below) |
| The Home Assistant Companion app | A URL that works away from home, usually the Nabu Casa remote URL |

**Plain http.** The sign-in code and the consent return travel in the URL. FAPI 2.0
requires https redirects except for loopback, so the form refuses a plain `http://`
address other than `localhost` or `127.0.0.1` until you tick **Allow a plain http:// URL**.
PKCE and DPoP stop a sniffed code being used elsewhere, but https is still the safer
choice. Each sign-in to a plain http URL logs a warning.

The URL is saved with the entry and only used again for reauth and reconfigure. If you
change how you reach Home Assistant, update it with **Reconfigure**.

## Costs

openfeed is free for consumers. As the registered app owner you are also the developer:
openfeed debits 1 credit per active grant when it is created and on each monthly
anniversary. The first 10 credits are free; see [openfeed pricing](https://openfeed.au/pricing).
When credits run out openfeed suspends the grant. Home Assistant raises a repair issue
and resumes automatically once you top up.

## Security

The integration follows openfeed's FAPI 2.0 profile:

- Pushed authorisation requests (PAR) with PKCE S256.
- `private_key_jwt` client authentication, PS256.
- DPoP sender-constrained tokens; every API call carries a fresh proof bound to the
  access token, with server nonces honoured.
- RFC 9207 `iss` check on the sign-in redirect, and id_token signature, audience and
  nonce verification.
- Diagnostics redact tokens, keys, account numbers, names, NMIs, meter IDs and
  equipment identifiers.

See [PRIVACY.md](PRIVACY.md) for what the integration collects and where it goes.

## Install

### HACS

Add `https://github.com/purcell-lab/ha-openfeed` as a custom repository of type
Integration, then install **openfeed Consumer Data Right**. A first install needs no
restart. Updates need a restart, because Home Assistant only loads new code at start-up.

### Manual

Copy `custom_components/openfeed` into your Home Assistant `config/custom_components/`
directory and restart.

## Development

```
pip install -r requirements-test.txt
pytest
ruff check .
```

The OAuth, DPoP, API client and usage aggregation modules (`crypto.py`, `oauth.py`,
`api.py`, `usage.py`) have no Home Assistant imports and are tested on their own.
Config flow and entry setup are tested with `pytest-homeassistant-custom-component`.

## Disclaimer

This is an independent project. It is not affiliated with or endorsed by openfeed,
Biza Pty Ltd, or the ACCC. openfeed's terms govern your use of openfeed.

## Licence

MIT
