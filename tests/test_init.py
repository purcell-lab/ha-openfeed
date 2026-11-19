"""Entry setup: coordinator, sensors, Energy statistics, revocation."""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
import time

import pytest

from homeassistant.components.recorder.statistics import get_last_statistics, list_statistic_ids
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.openfeed.const import DOMAIN
from custom_components.openfeed.usage import AEST

ISSUER = "https://auth.openfeed.au"
API = "https://api.openfeed.au"
CLIENT_ID = "app-0b7c2f4e-1a2b-4c3d-8e9f-0123456789ab"
ACC = "LTmpAcc="
METER = "MtrId="
NMI = "QB12345678"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    yield


@pytest.fixture
def entry(hass: HomeAssistant, key_pems) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=f"{CLIENT_ID}_user-1", title="openfeed CDR",
        data={"client_id": CLIENT_ID, "app_id": CLIENT_ID[4:], "redirect_base": "https://ha.example",
              "issuer": ISSUER, "scopes": ["openid", "offline_access", "openfeed-au:data:energy:read",
                                           "openfeed-au:data:banking:read"],
              "app_key_pem": key_pems[0], "dpop_key_pem": key_pems[1], "grant_id": "g-1",
              "subject": "user-1",
              "token": {"access_token": "at", "refresh_token": "rt", "expires_at": time.time() + 3600,
                        "grant_id": "g-1"}},
        options={"backfill_days": 3},
    )
    entry.add_to_hass(hass)
    return entry


def _usage_records():
    today = dt_util.now(AEST).date()
    out = []
    for back in (2, 1):
        day = (today - timedelta(days=back)).isoformat()
        out.append({"meterId": METER, "intervalDate": day, "reads": [
            {"meterId": METER, "registerSuffix": "E1", "readStartDate": day, "unitOfMeasure": "KWH",
             "readUType": "intervalRead", "intervalRead": {"readIntervalLength": 30,
                                                           "intervalReads": [0.5] * 48}},
            {"meterId": METER, "registerSuffix": "B1", "readStartDate": day, "unitOfMeasure": "KWH",
             "readUType": "intervalRead", "intervalRead": {"readIntervalLength": 30,
                                                           "intervalReads": [0.25] * 48}},
        ]})
    return out


DER = {
    "approvedCapacity": 10,
    "protectionMode": {"exportLimitKva": 5},
    "acConnections": [
        {"connectionIdentifier": 1, "count": 1, "manufacturerName": "Tesla",
         "inverterSeries": "Tesla Powerwall", "inverterDeviceCapacity": 5, "status": "ACTIVE",
         "derDevices": [{"deviceIdentifier": 2, "count": 10, "type": "SOLAR_PV",
                         "nominalRatedCapacity": 0.4, "status": "ACTIVE"}]},
        {"connectionIdentifier": 3, "count": 1, "manufacturerName": "Old Co",
         "inverterDeviceCapacity": 3, "status": "DECOMMISSIONED", "derDevices": []},
    ],
}


PLAN_DETAIL = {
    "fuelType": "ELECTRICITY",
    "fees": [{"type": "LATE_PAYMENT", "term": "FIXED", "amount": "12.00"}],
    "tariffPeriod": [
        {"type": "RETAIL_SERVICE", "displayName": "All year", "startDate": "01-01",
         "endDate": "12-31", "dailySupplyCharges": "1.00", "rateBlockUType": "singleRate",
         "singleRate": {"displayName": "Anytime", "rates": [{"unitPrice": "0.30"}]}},
    ],
    "solarFeedInTariff": [
        {"displayName": "Standard FiT", "payerType": "RETAILER", "tariffUType": "singleTariff",
         "singleTariff": {"amount": "0.05"}},
    ],
}

METER_DETAIL = {
    "meterId": METER, "nationalMeteringId": NMI, "status": "ACTIVE",
    "detectedReadType": "interval", "jurisdictionCode": "QLD",
    "servicePointClassification": "SMALL", "isGenerator": True,
}


# Personal fields a data holder can return; diagnostics must never carry them.
PRIVATE_LOCATION = {
    "addressUType": "simple",
    "simple": {"addressLine1": "1 Example Street", "city": "Exampleton", "postcode": "4999",
               "state": "QLD"},
}
PRIVATE_CONTACTS = [{"firstName": "Pat", "lastName": "Example", "middleNames": ["Q"],
                     "title": "Dr"}]


def _mock_api(aioclient_mock, revoked: bool = False, private: bool = False,
              usage_error: bool = False, der_error: bool = False, meters_error: bool = False):
    a = f"{API}/v1/energy/accounts/{ACC}"
    if revoked:
        aioclient_mock.get(f"{API}/v1/energy/accounts", status=403,
                           json={"code": "disclosure_grant_required"})
        return
    aioclient_mock.get(f"{API}/v1/energy/accounts", json={"data": [{"accountId": ACC, "displayName": "Home"}]})
    aioclient_mock.get(f"{a}/balance", json={"data": {"balance": "-123.45", "currency": "AUD"}})
    if meters_error:
        aioclient_mock.get(f"{a}/meters", status=500, json={"code": "internal"})
    else:
        aioclient_mock.get(f"{a}/meters", json={"data": [{"meterId": METER, "nationalMeteringId": NMI}]})
    aioclient_mock.get(f"{a}/invoices", json={"data": [
        {"invoiceNumber": "1", "issueDate": "2026-09-01", "dueDate": "2026-09-15", "invoiceAmount": "100.00", "paymentStatus": "PAID"},
        {"invoiceNumber": "2", "issueDate": "2026-10-01", "dueDate": "2026-10-15", "invoiceAmount": "88.10", "paymentStatus": "NOT_PAID",
         "meterIds": [METER]},
    ]})
    plan = {"planOverview": {"displayName": "Wholesale", "startDate": "2026-01-01"},
            "planDetail": PLAN_DETAIL}
    meter_detail = METER_DETAIL
    if private:
        plan = {**plan, "authorisedContacts": PRIVATE_CONTACTS}
        meter_detail = {**METER_DETAIL, "location": PRIVATE_LOCATION}
    aioclient_mock.get(a, json={"data": {"accountId": ACC, "plans": [plan]}})
    aioclient_mock.get(f"{a}/meters/{METER}", json={"data": meter_detail})
    aioclient_mock.get(f"{a}/billing", json={"data": [
        {"accountId": ACC, "executionDateTime": f"{dt_util.now(AEST).date().replace(day=1).isoformat()}T09:00:00+10:00",
         "transactionUType": "usage", "amount": 12.5, "description": "Usage"},
        {"accountId": ACC, "executionDateTime": f"{dt_util.now(AEST).date().replace(day=1).isoformat()}T09:00:00+10:00",
         "transactionUType": "usage", "amount": -4.0, "description": "Feed-in credit"},
        {"accountId": ACC, "executionDateTime": "2026-09-03T09:00:00+10:00",
         "transactionUType": "payment", "amount": -100.0},
    ]})
    if der_error:
        aioclient_mock.get(f"{a}/meters/{METER}/der", status=503, json={"code": "unavailable"})
    else:
        aioclient_mock.get(f"{a}/meters/{METER}/der", json={"data": DER})
    if usage_error:
        aioclient_mock.get(f"{a}/meters/{METER}/usage", status=500, json={"code": "internal"})
    else:
        aioclient_mock.get(f"{a}/meters/{METER}/usage", json={"data": _usage_records()})
    aioclient_mock.get(f"{API}/v1/banking/accounts", json={"data": [{"accountId": "B1=", "displayName": "Everyday"}]})
    aioclient_mock.get(f"{API}/v1/banking/accounts/B1=/balance", status=403,
                       json={"code": "balance_unavailable"})
    aioclient_mock.get(f"{API}/v1/banking/accounts/B1=", json={"data": {
        "accountId": "B1=", "displayName": "Everyday", "bsb": "062917", "accountNumber": "98765432",
        "depositRates": [{"rate": "0.0125", "rateType": "VARIABLE"},
                         {"rate": "0.04", "rateType": "BONUS"}]}})


async def test_setup_creates_sensors_and_statistics(hass, entry, aioclient_mock) -> None:
    _mock_api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    states = {s.entity_id: s for s in hass.states.async_all("sensor")}
    balance = next(s for e, s in states.items() if "balance" in e and s.attributes.get("unit_of_measurement") == "AUD"
                   and s.state not in ("unknown", "unavailable"))
    assert float(balance.state) == -123.45
    meter = f"sensor.meter_{NMI.lower()}"
    assert float(states[f"{meter}_latest_day_grid_consumption"].state) == 24.0
    assert float(states[f"{meter}_latest_day_grid_export"].state) == 12.0
    # 0.5 kWh per 30 minutes is 1 kW; 0.25 kWh is 0.5 kW.
    assert float(states[f"{meter}_latest_day_peak_grid_import"].state) == 1.0
    assert float(states[f"{meter}_latest_day_peak_grid_export"].state) == 0.5
    assert float(states[f"{meter}_der_solar_capacity"].state) == 4.0
    issues = states[f"{meter}_der_register_issues"]
    assert int(issues.state) == 3
    assert "decommissioned_records_listed" in issues.attributes["issues"]
    assert states["sensor.energy_home_plan"].state == "Wholesale"
    assert float(states["sensor.energy_home_month_to_date_charges"].state) == 12.5
    assert float(states["sensor.energy_home_month_to_date_credits"].state) == 4.0
    assert float(states["sensor.energy_home_last_statement_charges"].state) == 12.5
    statement = states["sensor.energy_home_last_statement_credits"]
    assert float(statement.state) == 4.0
    assert statement.attributes["net"] == 8.5
    assert float(states[f"{meter}_last_30_days_grid_import"].state) == 48.0
    assert float(states[f"{meter}_last_30_days_grid_export"].state) == 24.0
    history = states[f"{meter}_usage_history"]
    assert int(history.state) == 2
    assert f"openfeed:nmi_{NMI.lower()}_consumption" in history.attributes["statistics"]

    # The bills calendar holds one event per invoice.
    assert "calendar.energy_home_bills" in {s.entity_id for s in hass.states.async_all("calendar")}
    result = await hass.services.async_call(
        "calendar", "get_events",
        {"entity_id": "calendar.energy_home_bills", "start_date_time": "2026-08-01T00:00:00+10:00",
         "end_date_time": "2026-11-01T00:00:00+10:00"},
        blocking=True, return_response=True,
    )
    events = result["calendar.energy_home_bills"]["events"]
    assert [e["summary"] for e in events] == [
        "Energy bill AUD 100.00 (PAID)", "Energy bill AUD 88.10 (NOT_PAID)"]
    assert events[0]["start"] == "2026-09-01" and events[0]["end"] == "2026-09-16"

    # The meter device links to the Energy settings.
    from homeassistant.helpers import device_registry as dr
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, f"meter_nmi_{NMI.lower()}"), entry.entry_id)
    assert device.configuration_url == "homeassistant://config/energy"
    invoice = next(s for e, s in states.items() if "invoice_amount" in e)
    assert float(invoice.state) == 88.10
    assert states["sensor.energy_home_latest_invoice_status"].state == "NOT_PAID"
    assert states["sensor.bank_everyday_current_balance"].state == "unavailable"

    await async_wait_recording_done(hass)
    stat_id = f"openfeed:nmi_{NMI.lower()}_consumption"
    last = await hass.async_add_executor_job(get_last_statistics, hass, 1, stat_id, True, {"sum"})
    assert last[stat_id][0]["sum"] == pytest.approx(48.0)
    power_id = f"openfeed:nmi_{NMI.lower()}_consumption_power"
    last = await hass.async_add_executor_job(get_last_statistics, hass, 1, power_id, True, {"max", "mean"})
    assert last[power_id][0]["max"] == pytest.approx(1.0)
    stats = await hass.async_add_executor_job(list_statistic_ids, hass)
    ids = {s["statistic_id"] for s in stats}
    assert any(i.endswith("_charges") for i in ids)
    assert any(i.endswith("_invoiced") for i in ids)
    assert any(i.endswith("_payments") for i in ids)
    assert any(i.endswith("_credits") for i in ids)
    # The legacy signed payments series was rebuilt once.
    assert entry.data["money_stats_version"] == 3

    # Every API call carried a DPoP-bound token.
    for method, url, data, headers in aioclient_mock.mock_calls:
        if str(url).startswith(API):
            assert headers["Authorization"] == "DPoP at"
            assert headers["DPoP"]


async def test_usage_failure_on_first_load_keeps_other_entities(hass, entry, aioclient_mock) -> None:
    """A usage 500 for one meter no longer fails the whole refresh."""
    _mock_api(aioclient_mock, usage_error=True)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    meter = f"sensor.meter_{NMI.lower()}"
    assert hass.states.get("sensor.energy_home_plan").state == "Wholesale"
    assert float(hass.states.get(f"{meter}_der_solar_capacity").state) == 4.0
    fetched = hass.states.get(f"{meter}_usage_last_fetched")
    assert fetched.attributes["usage_status"] == "error"
    assert fetched.attributes["der_status"] == "ok"


async def _refresh_with(hass, entry, aioclient_mock, **errors) -> None:
    """Re-mock the API with errors and run a refresh that re-fetches usage."""
    coordinator = entry.runtime_data
    for md in coordinator._usage_cache.values():
        md.usage_fetched_at = dt_util.utcnow() - timedelta(hours=4)
    coordinator._meter_detail_cache.clear()
    aioclient_mock.clear_requests()
    _mock_api(aioclient_mock, **errors)
    await coordinator.async_refresh()
    await hass.async_block_till_done()


async def test_later_usage_and_der_failures_keep_last_good_data(hass, entry, aioclient_mock) -> None:
    _mock_api(aioclient_mock)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    meter = f"sensor.meter_{NMI.lower()}"
    fetched_before = hass.states.get(f"{meter}_usage_last_fetched").state

    await _refresh_with(hass, entry, aioclient_mock, usage_error=True, der_error=True)
    assert entry.runtime_data.last_update_success
    assert float(hass.states.get(f"{meter}_latest_day_grid_consumption").state) == 24.0
    assert float(hass.states.get(f"{meter}_der_solar_capacity").state) == 4.0
    fetched = hass.states.get(f"{meter}_usage_last_fetched")
    # The last successful fetch time is kept (the helper backdated it 4 hours).
    kept = dt_util.parse_datetime(fetched.state)
    expected = dt_util.parse_datetime(fetched_before) - timedelta(hours=4)
    assert abs(kept - expected) <= timedelta(seconds=5)
    assert fetched.attributes["usage_status"] == "error"
    assert fetched.attributes["der_status"] == "error"

    # The failed usage is retried on the next poll, and recovers.
    aioclient_mock.clear_requests()
    _mock_api(aioclient_mock)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert any("/usage" in str(call[1]) for call in aioclient_mock.mock_calls)
    assert hass.states.get(f"{meter}_usage_last_fetched").attributes["usage_status"] == "ok"


async def test_meters_list_failure_keeps_cached_meters(hass, entry, aioclient_mock) -> None:
    _mock_api(aioclient_mock)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _refresh_with(hass, entry, aioclient_mock, meters_error=True)
    assert entry.runtime_data.last_update_success
    meter = f"sensor.meter_{NMI.lower()}"
    assert float(hass.states.get(f"{meter}_latest_day_grid_consumption").state) == 24.0
    assert hass.states.get(f"{meter}_usage_last_fetched").attributes["meters_source"] == "cache"


async def test_meters_list_failure_without_cache_fails_refresh(hass, entry, aioclient_mock) -> None:
    _mock_api(aioclient_mock, meters_error=True)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_revocation_triggers_reauth(hass, entry, aioclient_mock) -> None:
    _mock_api(aioclient_mock, revoked=True)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == "reauth"
    assert entry.data["grant_id"] is None
    # The old tokens are not kept once the grant has gone.
    assert "token" not in entry.data


def _mock_discovery(aioclient_mock, revocation: bool = True):
    doc = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/auth",
        "token_endpoint": f"{ISSUER}/token",
        "pushed_authorization_request_endpoint": f"{ISSUER}/request",
        "jwks_uri": f"{ISSUER}/jwks",
    }
    if revocation:
        doc["revocation_endpoint"] = f"{ISSUER}/token/revocation"
    aioclient_mock.get(f"{ISSUER}/.well-known/openid-configuration", json=doc)


async def test_remove_entry_revokes_refresh_token(hass, entry, aioclient_mock) -> None:
    _mock_api(aioclient_mock)
    _mock_discovery(aioclient_mock)
    aioclient_mock.post(f"{ISSUER}/token/revocation", status=200, text="")
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert len(posts) == 1
    form = posts[0][2]
    assert form["token"] == "rt"
    assert form["token_type_hint"] == "refresh_token"
    assert form["client_id"] == CLIENT_ID
    assert form["client_assertion"]


async def test_remove_entry_survives_revocation_failure(hass, entry, aioclient_mock, caplog) -> None:
    _mock_api(aioclient_mock)
    _mock_discovery(aioclient_mock)
    aioclient_mock.post(f"{ISSUER}/token/revocation", status=503, json={"error": "temporarily_unavailable"})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.config_entries.async_get_entry(entry.entry_id) is None
    assert "Could not revoke the openfeed refresh token" in caplog.text


async def test_remove_entry_without_revocation_endpoint(hass, entry, aioclient_mock) -> None:
    _mock_api(aioclient_mock)
    _mock_discovery(aioclient_mock, revocation=False)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.config_entries.async_get_entry(entry.entry_id) is None
    assert not [c for c in aioclient_mock.mock_calls if c[0] == "POST"]


async def test_credit_exhausted_raises_issue(hass, entry, aioclient_mock) -> None:
    aioclient_mock.get(f"{API}/v1/energy/accounts", status=402, json={"code": "credit_exhausted"})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert ir.async_get(hass).async_get_issue(DOMAIN, "credit_exhausted")


async def test_diagnostics_redacts(hass, entry, aioclient_mock) -> None:
    from custom_components.openfeed.diagnostics import async_get_config_entry_diagnostics

    _mock_api(aioclient_mock)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    for secret in ("BEGIN PRIVATE KEY", "'rt'", NMI, ACC, METER, "user-1", "Home", "Feed-in credit"):
        assert secret not in text
    lines = diag["energy"]["account_0"]["billing_lines"]
    assert {line["amount"] for line in lines} == {12.5, -4.0, -100.0}
    assert diag["entry"]["data"]["redirect_base"] == "**REDACTED**"
    totals = diag["energy"]["account_0"]["history"]["series_totals"]
    assert totals["payments"] == 100.0
    assert "refunds" not in totals


async def test_diagnostics_redact_address_and_contacts(hass, entry, aioclient_mock) -> None:
    """The meter detail location and the plan's authorised contacts never leak."""
    from custom_components.openfeed.diagnostics import async_get_config_entry_diagnostics

    _mock_api(aioclient_mock, private=True)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    text = str(await async_get_config_entry_diagnostics(hass, entry))
    for secret in ("Example Street", "Exampleton", "4999", "Pat", "'Example'", "https://ha.example"):
        assert secret not in text
    # The redacted keys are still visible, so the shape stays useful.
    assert "'location': '**REDACTED**'" in text
    assert "'authorisedContacts': '**REDACTED**'" in text


async def test_plan_sensor_removed_when_no_plan(hass, entry, aioclient_mock) -> None:
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor", DOMAIN, f"{entry.unique_id}_{ACC}_plan", config_entry=entry
    )
    _mock_api_no_plan(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(old.entity_id) is None
    assert hass.states.get("sensor.energy_home_month_to_date_charges") is not None
    assert hass.states.get("sensor.energy_home_import_rate") is None
    assert hass.states.get("sensor.energy_home_feed_in_rate") is None


async def test_tariff_sensors(hass, entry, aioclient_mock) -> None:
    _mock_api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rate = hass.states.get("sensor.energy_home_import_rate")
    assert float(rate.state) == pytest.approx(0.33)
    assert rate.attributes["unit_of_measurement"] == "AUD/kWh"
    assert rate.attributes["rate_ex_gst"] == 0.30
    assert hass.states.get("sensor.energy_home_tariff_period").state == "single_rate"
    fit = hass.states.get("sensor.energy_home_feed_in_rate")
    assert float(fit.state) == 0.05
    assert fit.attributes["tariffs"][0]["payer"] == "RETAILER"
    supply = hass.states.get("sensor.energy_home_daily_supply_charge")
    assert float(supply.state) == pytest.approx(1.10)
    assert supply.attributes["unit_of_measurement"] == "AUD/d"
    assert hass.states.get("sensor.energy_home_demand_charge") is None
    # A flat plan never changes within the horizon.
    assert hass.states.get("sensor.energy_home_next_tariff_change").state == "unknown"
    plan = hass.states.get("sensor.energy_home_plan")
    assert plan.attributes["fees"][0]["type"] == "LATE_PAYMENT"


async def test_meter_detail_sensors(hass, entry, aioclient_mock) -> None:
    from homeassistant.helpers import entity_registry as er

    _mock_api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    meter = f"sensor.meter_{NMI.lower()}"
    status = hass.states.get(f"{meter}_meter_status")
    assert status.state == "active"
    assert status.attributes["detail_source"] == "detail"
    assert status.attributes["is_generator"] is True
    assert hass.states.get(f"{meter}_meter_read_type").state == "interval"
    assert hass.states.get(f"{meter}_jurisdiction").state == "QLD"
    assert hass.states.get(f"{meter}_service_point_classification").state == "small"
    assert hass.states.get(f"{meter}_generator").state == "yes"
    # The NMI sensor exists but is disabled by default.
    nmi = er.async_get(hass).async_get(f"{meter}_nmi")
    assert nmi is not None and nmi.disabled_by is not None


async def test_meter_detail_failure_falls_back_to_list(hass, entry, aioclient_mock) -> None:
    _mock_api(aioclient_mock)
    url = f"{API}/v1/energy/accounts/{ACC}/meters/{METER}"
    aioclient_mock._mocks = [m for m in aioclient_mock._mocks if str(m.url) != url]
    aioclient_mock.get(url, status=500, json={"code": "internal"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    meter = f"sensor.meter_{NMI.lower()}"
    status = hass.states.get(f"{meter}_meter_status")
    assert status.attributes["detail_source"] == "list"
    assert hass.states.get(f"{meter}_generator").state == "unknown"


def _mock_api_no_plan(aioclient_mock):
    _mock_api(aioclient_mock)
    a = f"{API}/v1/energy/accounts/{ACC}"
    # The first registered mock wins, so replace the detail mock.
    aioclient_mock._mocks = [m for m in aioclient_mock._mocks if str(m.url) != a]
    aioclient_mock.get(a, json={"data": {"accountId": ACC, "plans": None}})


async def test_sidebar_dashboard(hass, entry, aioclient_mock, hass_ws_client) -> None:
    from homeassistant.components.frontend import DATA_PANELS
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "lovelace", {})
    _mock_api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    panel = hass.data[DATA_PANELS]["openfeed"]
    assert panel.sidebar_title == "Energy data"
    assert panel.component_name == "lovelace"

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "lovelace/config", "url_path": "openfeed"})
    msg = await client.receive_json()
    assert msg["success"], msg
    config = msg["result"]
    cards = config["views"][0]["cards"]
    titles = [c.get("title") for c in cards]
    assert "Grid power, hourly mean and peak (last 7 days)" in titles
    assert "Monthly charges, credits and payments" in titles
    # Every entity the dashboard names exists, and no NMI leaks into names.
    states = {s.entity_id for s in hass.states.async_all()}
    for card in cards:
        for row in card.get("entities", []):
            ref = row["entity"] if isinstance(row, dict) else row
            if ref.startswith("openfeed:"):
                continue
            assert ref in states, ref
            if isinstance(row, dict):
                assert NMI not in row["name"]
    power = next(c for c in cards if c["title"].startswith("Grid power"))
    assert power["entities"][0]["entity"] == f"openfeed:nmi_{NMI.lower()}_consumption_power"
    # Two-year charts cover every day the CDR shares.
    energy_2y = next(c for c in cards if c["title"].startswith("Monthly grid energy, 2 years"))
    assert energy_2y["period"] == "month" and energy_2y["days_to_show"] == 730
    assert [e["entity"] for e in energy_2y["entities"]] == [
        f"openfeed:nmi_{NMI.lower()}_consumption", f"openfeed:nmi_{NMI.lower()}_export"]
    power_2y = next(c for c in cards if c["title"].startswith("Weekly grid power"))
    assert power_2y["period"] == "week" and power_2y["days_to_show"] == 730
    assert power_2y["stat_types"] == ["mean", "max"]

    # Switching the option off removes the panel on reload.
    hass.config_entries.async_update_entry(entry, options={"sidebar_dashboard": False})
    await hass.async_block_till_done()
    assert "openfeed" not in hass.data[DATA_PANELS]


async def test_dashboard_skipped_without_lovelace(hass, entry, aioclient_mock) -> None:
    _mock_api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_legacy_payments_rebuilt_without_stale_sum(hass, entry, aioclient_mock) -> None:
    from datetime import UTC, datetime

    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import async_add_external_statistics

    from custom_components.openfeed.statistics import account_key

    stat_id = f"openfeed:{account_key(ACC)}_payments"
    async_add_external_statistics(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE, has_sum=True, name="old", source=DOMAIN,
            statistic_id=stat_id, unit_class=None, unit_of_measurement="AUD",
        ),
        [StatisticData(start=datetime(2026, 3, 4, 1, tzinfo=UTC), state=6151.22, sum=6151.22)],
    )
    await async_wait_recording_done(hass)
    _mock_api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    last = await hass.async_add_executor_job(get_last_statistics, hass, 1, stat_id, True, {"sum"})
    assert last[stat_id][0]["sum"] == pytest.approx(100.0)


async def test_rebuild_ignores_earlier_running_sum(hass) -> None:
    from datetime import UTC, datetime

    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import async_add_external_statistics

    from custom_components.openfeed.statistics import async_import_money

    stat_id = "openfeed:acct_test_payments"
    async_add_external_statistics(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE, has_sum=True, name="old", source=DOMAIN,
            statistic_id=stat_id, unit_class=None, unit_of_measurement="AUD",
        ),
        [StatisticData(start=datetime(2026, 3, 4, 1, tzinfo=UTC), state=50.0, sum=50.0)],
    )
    await async_wait_recording_done(hass)
    hour = datetime(2026, 6, 9, 0, tzinfo=UTC)
    await async_import_money(hass, "acct_test", "x", {"payments": {hour: 14.27}}, "AUD")
    await async_wait_recording_done(hass)
    last = await hass.async_add_executor_job(get_last_statistics, hass, 1, stat_id, True, {"sum"})
    assert last[stat_id][0]["sum"] == pytest.approx(64.27)
    await async_import_money(
        hass, "acct_test", "x", {"payments": {hour: 14.27}}, "AUD", frozenset({"payments"})
    )
    await async_wait_recording_done(hass)
    last = await hass.async_add_executor_job(get_last_statistics, hass, 1, stat_id, True, {"sum"})
    assert last[stat_id][0]["sum"] == pytest.approx(14.27)


async def test_legacy_entry_keeps_nmi_keys(hass, entry, aioclient_mock) -> None:
    """Entries made before minor version 2 keep their statistic IDs."""
    _mock_api(aioclient_mock)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.minor_version == 2
    assert entry.data["meter_key_scheme"] == "legacy"
    assert "meter_key_salt" not in entry.data
    await async_wait_recording_done(hass)
    ids = {s["statistic_id"] for s in await hass.async_add_executor_job(list_statistic_ids, hass)}
    assert f"openfeed:nmi_{NMI.lower()}_consumption" in ids


async def test_new_entry_keeps_nmi_out_of_ids_and_names(hass, entry, aioclient_mock) -> None:
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    hass.config_entries.async_update_entry(entry, minor_version=2)
    _mock_api(aioclient_mock)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    salt = entry.data["meter_key_salt"]
    assert len(salt) == 32 and "meter_key_scheme" not in entry.data
    await async_wait_recording_done(hass)

    nmi = NMI.lower()
    stats = await hass.async_add_executor_job(list_statistic_ids, hass)
    meter_stats = [s for s in stats if "consumption" in s["statistic_id"]]
    assert meter_stats
    for stat in stats:
        assert nmi not in stat["statistic_id"].lower()
        assert nmi not in (stat.get("name") or "").lower()
    for ent in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        assert nmi not in ent.entity_id and nmi not in ent.unique_id.lower()
    for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id):
        assert all(nmi not in ident.lower() for _, ident in device.identifiers)
        assert nmi not in (device.name or "").lower()
        assert nmi not in (device.serial_number or "").lower()
    for state in hass.states.async_all():
        assert nmi not in str(state.attributes).lower()

    # The salt never changes once picked.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.data["meter_key_salt"] == salt


async def test_bank_detail_sensors_without_transactions(hass, entry, aioclient_mock) -> None:
    _mock_api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    states = {s.entity_id: s for s in hass.states.async_all("sensor")}
    rate = states["sensor.bank_everyday_interest_rate"]
    assert float(rate.state) == 1.25
    assert rate.attributes["rate_kind"] == "deposit"
    assert [r["rate"] for r in rate.attributes["all_rates"]] == [1.25, 4.0]
    # Transactions are opt in: no totals and no request without the option.
    assert "sensor.bank_everyday_month_to_date_money_in" not in states
    assert not any("/transactions" in str(call[1]) for call in aioclient_mock.mock_calls)
    # The unmasked BSB and account number never reach a state or attribute.
    for state in states.values():
        assert "062917" not in str(state.attributes) and "98765432" not in str(state.attributes)


async def test_bank_transaction_totals_opt_in(hass, entry, aioclient_mock) -> None:
    from custom_components.openfeed.diagnostics import async_get_config_entry_diagnostics

    hass.config_entries.async_update_entry(entry, options={"backfill_days": 3, "bank_transactions": True})
    _mock_api(aioclient_mock)
    today = dt_util.now(AEST).date()
    aioclient_mock.get(f"{API}/v1/banking/accounts/B1=/transactions", json={"data": [
        {"transactionId": "t1", "accountId": "B1=", "amount": "4321.00",
         "transactionDate": today.isoformat(), "transactionType": "TRANSFER_INCOMING",
         "status": "POSTED", "description": "Pay from employer"},
        {"transactionId": "t2", "accountId": "B1=", "amount": "-30.00",
         "transactionDate": today.isoformat(), "transactionType": "PAYMENT",
         "status": "POSTED", "merchantName": "Corner Store"},
        {"transactionId": "t3", "accountId": "B1=", "amount": "-2.50",
         "transactionDate": today.isoformat(), "transactionType": "FEE", "status": "POSTED"},
    ]})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    states = {s.entity_id: s for s in hass.states.async_all("sensor")}
    assert float(states["sensor.bank_everyday_month_to_date_money_in"].state) == 4321.0
    assert float(states["sensor.bank_everyday_month_to_date_money_out"].state) == 32.5
    assert float(states["sensor.bank_everyday_last_30_days_money_in"].state) == 4321.0
    assert float(states["sensor.bank_everyday_month_to_date_fees"].state) == 2.5
    assert float(states["sensor.bank_everyday_month_to_date_interest"].state) == 0.0
    text = str([s.attributes for s in states.values()])
    assert "employer" not in text and "Corner Store" not in text
    diag = str(await async_get_config_entry_diagnostics(hass, entry))
    for secret in ("employer", "Corner Store", "062917", "98765432", "4321.0"):
        assert secret not in diag


async def test_diagnostics_show_usage_requests(hass, entry, aioclient_mock) -> None:
    from custom_components.openfeed.diagnostics import async_get_config_entry_diagnostics

    _mock_api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    runs = diag["energy"]["account_0"]["meters"][0]["usage_requests"]
    assert len(runs) == 1 and runs[0]["kind"] == "backfill"
    window = runs[0]["windows"][0]
    assert window["records"] == 2
    assert window["first_interval_date"] <= window["last_interval_date"]
    request = window["requests"][0]
    assert request["status"] == 200 and request["interaction_id"]
    assert request["endpoint"].startswith("/v1/energy/accounts/{id}/meters/{id}/usage")
    assert ACC not in str(runs) and METER not in str(runs)
    samples = runs[0]["samples"]
    assert samples["days"] >= 1 and (samples["data_day"] or samples["empty_day"])


OTHER_GRID = {
    "type": "grid", "stat_energy_from": "sensor.house_grid_import", "stat_energy_to": None,
    "stat_cost": None, "entity_energy_price": None, "number_energy_price": None,
    "stat_compensation": None, "entity_energy_price_export": None,
    "number_energy_price_export": None, "cost_adjustment_day": 0.0,
}
SOLAR = {"type": "solar", "stat_energy_from": "sensor.solar_energy", "config_entry_solar_forecast": None}


async def _energy_prefs(hass, seed=None):
    from homeassistant.components.energy.data import async_get_manager
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "energy", {})
    manager = await async_get_manager(hass)
    if seed is not None:
        await manager.async_update({"energy_sources": seed})
    return manager


@pytest.mark.parametrize(
    ("mode", "expected_types"),
    [("add", ["grid", "solar", "grid"]), ("replace", ["solar", "grid"])],
)
async def test_energy_dashboard_choice_applied_once(hass, entry, aioclient_mock, mode, expected_types) -> None:
    manager = await _energy_prefs(hass, [OTHER_GRID, SOLAR])
    hass.config_entries.async_update_entry(entry, options={"backfill_days": 3, "energy_dashboard": mode})
    _mock_api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    sources = manager.data["energy_sources"]
    assert [s["type"] for s in sources] == expected_types
    ours = sources[-1]
    assert ours["stat_energy_from"] == f"openfeed:nmi_{NMI.lower()}_consumption"
    assert ours["stat_energy_to"] == f"openfeed:nmi_{NMI.lower()}_export"
    assert ours["entity_energy_price"] is None and ours["name"].startswith("openfeed meter")
    # The choice is reset, so a reload or later manual edit is not undone.
    assert entry.options["energy_dashboard"] == "no_change"
    assert entry.options["backfill_days"] == 3
    assert entry.state is ConfigEntryState.LOADED
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert manager.data["energy_sources"] == sources


async def test_energy_dashboard_add_is_idempotent(hass, entry, aioclient_mock) -> None:
    from custom_components.openfeed.energy import grid_sources, merge_sources

    _mock_api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    ours = grid_sources(entry.runtime_data.data.energy)
    assert len(ours) == 1
    once = merge_sources([SOLAR], ours, "add")
    assert once == [SOLAR, ours[0]]
    assert merge_sources(once, ours, "add") is None
    assert merge_sources(once, ours, "replace") is None
    assert merge_sources([OTHER_GRID], ours, "no_change") is None


async def test_energy_dashboard_left_alone_without_energy(hass, entry, aioclient_mock, caplog) -> None:
    hass.config_entries.async_update_entry(entry, options={"energy_dashboard": "add"})
    _mock_api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert "Energy integration is not loaded" in caplog.text
    assert entry.options["energy_dashboard"] == "no_change"



async def test_backfill_skips_windows_already_returned(hass, entry, aioclient_mock) -> None:
    """openfeed ignores oldestDate; later windows are not requested once covered."""
    from custom_components.openfeed.diagnostics import async_get_config_entry_diagnostics

    hass.config_entries.async_update_entry(entry, options={"backfill_days": 70})
    # The usage endpoint returns all 70 days whatever range is asked for.
    today = dt_util.now(AEST).date()
    days = [(today - timedelta(days=back)).isoformat() for back in range(70, 0, -1)]
    records = [
        {"meterId": METER, "intervalDate": day, "reads": [
            {"meterId": METER, "registerSuffix": "E1", "readStartDate": day, "unitOfMeasure": "KWH",
             "readUType": "intervalRead",
             "intervalRead": {"readIntervalLength": 30, "intervalReads": [0.5] * 48}}]}
        for day in days
    ]
    _mock_api(aioclient_mock)
    usage_url = f"{API}/v1/energy/accounts/{ACC}/meters/{METER}/usage"
    aioclient_mock._mocks = [m for m in aioclient_mock._mocks if str(m.url) != usage_url]
    aioclient_mock.get(usage_url, json={"data": records})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    usage_calls = [c for c in aioclient_mock.mock_calls if str(c[1]).startswith(usage_url)]
    assert len(usage_calls) == 2
    diag = await async_get_config_entry_diagnostics(hass, entry)
    windows = diag["energy"]["account_0"]["meters"][0]["usage_requests"][0]["windows"]
    assert len(windows) == 3
    assert "skipped" not in windows[0] and windows[0]["records"] == 70
    # The middle window was wholly returned by the first, so it is not requested.
    assert windows[1]["skipped"] == "covered" and windows[1]["requests"] == []
    # The last window only asks for today, the one day the first did not return.
    assert windows[2]["requested_oldest"] == windows[2]["requested_newest"] == today.isoformat()
    # Every day the first window returned is imported.
    meter = next(iter(entry.runtime_data.data.energy[ACC].meters.values()))
    assert len(meter.daily_history["consumption"]) == 70


async def test_usage_history_counts_long_term_statistics(hass, entry, aioclient_mock) -> None:
    """The usage history sensor counts days in the recorder, beyond the 400-day summary."""
    from datetime import UTC

    from custom_components.openfeed.statistics import async_import_meter

    _mock_api(aioclient_mock)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    md = next(iter(coordinator.data.energy[ACC].meters.values()))
    # An hour 500 days back: older than the in-memory summary keeps.
    old_day = dt_util.now(AEST).date() - timedelta(days=500)
    old_hour = datetime.combine(old_day, dtime(1), AEST).astimezone(UTC)
    await async_import_meter(hass, md.key, "openfeed test", {"consumption": {old_hour: 1.0}})
    # Force the next refresh to re-read usage, which updates the coverage.
    for cached in coordinator._usage_cache.values():
        cached.usage_fetched_at = dt_util.utcnow() - timedelta(hours=4)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    history = hass.states.get(f"sensor.meter_{NMI.lower()}_usage_history")
    assert int(history.state) == 3
    assert history.attributes["oldest"] == old_day.isoformat()
    assert history.attributes["source"] == "statistics"
    assert history.attributes["summary_days"] == 2
