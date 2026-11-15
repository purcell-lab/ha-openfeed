"""Banking detail and transaction totals."""

from datetime import date

from custom_components.openfeed.banking import (
    account_rate,
    strip_detail,
    summarise_transactions,
    transaction_day,
    window_start,
)


def test_strip_detail_drops_unmasked_numbers() -> None:
    detail = {"accountId": "A", "bsb": "062000", "accountNumber": "12345678", "fees": []}
    assert strip_detail(detail) == {"accountId": "A", "fees": []}
    assert strip_detail(None) is None


def test_account_rate_prefers_base_deposit_rate() -> None:
    rate = account_rate({"depositRates": [
        {"rate": "0.04", "rateType": "BONUS"},
        {"rate": "0.0125", "rateType": "VARIABLE"},
    ]})
    assert rate is not None
    assert (rate.kind, rate.rate, rate.rate_type) == ("deposit", 1.25, "VARIABLE")
    assert [r["rate"] for r in rate.rates] == [4.0, 1.25]


def test_account_rate_lending_and_missing() -> None:
    rate = account_rate({"lendingRates": [{"rate": "0.0619", "rateType": "VARIABLE"}],
                         "depositRates": [{"rate": "0.001"}]})
    assert rate is not None and rate.kind == "lending" and rate.rate == 6.19
    assert account_rate({"depositRates": [{"rate": "n/a"}]}) is None
    assert account_rate({}) is None
    assert account_rate(None) is None


def test_transaction_day_falls_back_through_four_fields() -> None:
    assert transaction_day({"transactionDate": "2026-11-02"}) == date(2026, 11, 2)
    assert transaction_day({"valueDate": None, "postedDateTime": "2026-11-03T01:00:00Z"}) == date(2026, 11, 3)
    assert transaction_day({"executionDateTime": "2026-11-04T09:00:00+10:00"}) == date(2026, 11, 4)
    assert transaction_day({"transactionDate": "bad", "valueDate": "2026-11-05"}) == date(2026, 11, 5)
    assert transaction_day({}) is None


def test_window_start_covers_month_and_30_days() -> None:
    assert window_start(date(2026, 11, 15)) == date(2026, 10, 17)
    assert window_start(date(2026, 12, 31)) == date(2026, 12, 1)


def test_summarise_transactions() -> None:
    today = date(2026, 11, 15)
    txns = [
        {"transactionId": "1", "amount": "2500.00", "transactionDate": "2026-11-01",
         "transactionType": "TRANSFER_INCOMING", "status": "POSTED", "description": "Salary"},
        {"transactionId": "2", "amount": "-120.50", "postedDateTime": "2026-11-10T03:00:00Z",
         "transactionType": "PAYMENT", "status": "POSTED", "merchantName": "Shop"},
        {"transactionId": "2", "amount": "-120.50", "postedDateTime": "2026-11-10T03:00:00Z",
         "transactionType": "PAYMENT", "status": "POSTED"},  # duplicate page row
        {"transactionId": "3", "amount": "3.21", "valueDate": "2026-11-12",
         "transactionType": "INTEREST_PAID", "status": "POSTED"},
        {"transactionId": "4", "amount": "-5.00", "transactionDate": "2026-11-13",
         "transactionType": "FEE", "status": "POSTED"},
        {"transactionId": "5", "amount": "-40.00", "transactionDate": "2026-10-20",
         "transactionType": "DIRECT_DEBIT", "status": "POSTED"},
        {"transactionId": "6", "amount": "-99.00", "transactionDate": "2026-11-14", "status": "PENDING"},
        {"transactionId": "7", "amount": "-1.00"},  # no date
        {"transactionId": "8", "transactionDate": "2026-11-14"},  # no amount
        {"transactionId": "9", "amount": "-7.00", "transactionDate": "2026-10-01"},  # outside 30 days
    ]
    s = summarise_transactions(txns, today)
    assert s.month_in == 2503.21
    assert s.month_out == 125.5
    assert s.last_30_in == 2503.21
    assert s.last_30_out == 165.5
    assert s.month_interest_paid == 3.21
    assert s.month_fees == 5.0
    assert s.month_interest_charged == 0.0
    assert (s.posted, s.pending, s.skipped) == (6, 1, 2)
    assert s.latest_day == date(2026, 11, 13)
    assert s.by_type == {"FEE": -5.0, "INTEREST_PAID": 3.21, "PAYMENT": -120.5,
                         "TRANSFER_INCOMING": 2500.0}
    # Nothing but totals and counts is kept.
    assert "Salary" not in str(s) and "Shop" not in str(s)
