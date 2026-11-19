"""Interval read aggregation."""

from __future__ import annotations

from datetime import UTC, date, datetime

from custom_components.openfeed.usage import (
    CONSUMPTION,
    CONTROLLED_LOAD,
    EXPORT,
    cumulative,
    sample_records,
    summarise,
    usage_windows,
)


def _record(day, values, suffix="E1", controlled=None, unit="KWH", qualities=None):
    read = {
        "meterId": "M1", "registerId": "R", "registerSuffix": suffix,
        "readStartDate": day, "unitOfMeasure": unit, "readUType": "intervalRead",
        "intervalRead": {"readIntervalLength": 30, "intervalReads": values,
                         "readQualities": qualities or []},
    }
    if controlled is not None:
        read["controlledLoad"] = controlled
    return {"meterId": "M1", "intervalDate": day, "reads": [read]}


def test_aest_midnight_origin_and_hourly_sum():
    s = summarise([_record("2026-11-10", [0.5, 0.25, 1.0])])
    # Midnight AEST is 14:00 UTC the previous day; first two half hours share an hour.
    hours = s.hourly[CONSUMPTION]
    assert hours[datetime(2026, 11, 9, 14, tzinfo=UTC)] == 0.75
    assert hours[datetime(2026, 11, 9, 15, tzinfo=UTC)] == 1.0
    assert s.daily[CONSUMPTION][date(2026, 11, 10)] == 1.75


def test_negative_values_are_export():
    s = summarise([_record("2026-11-10", [1.0, -2.0])])
    assert sum(s.hourly[CONSUMPTION].values()) == 1.0
    assert sum(s.hourly[EXPORT].values()) == 2.0


def test_b_suffix_positive_is_export():
    s = summarise([_record("2026-11-10", [3.0], suffix="B1")])
    assert sum(s.hourly[EXPORT].values()) == 3.0
    assert CONSUMPTION not in s.hourly


def test_controlled_load_series():
    s = summarise([_record("2026-11-10", [1.5], suffix="E2", controlled=True)])
    assert sum(s.hourly[CONTROLLED_LOAD].values()) == 1.5


def test_wh_scaled_and_duplicates_deduplicated():
    rec = _record("2026-11-10", [500.0], unit="WH")
    s = summarise([rec, rec])
    assert sum(s.hourly[CONSUMPTION].values()) == 0.5


def test_quality_counts_and_basic_reads_skipped():
    rec = _record("2026-11-10", [1, 1, 1], qualities=[{"startInterval": 2, "endInterval": 3,
                                                       "quality": "SUBSTITUTE"}])
    basic = {"meterId": "M1", "intervalDate": "2026-11-10",
             "reads": [{"readUType": "basicRead", "basicRead": {"value": 9}}]}
    s = summarise([rec, basic])
    assert s.substituted_intervals == 2
    assert s.skipped_reads == 1


def test_windows_are_at_most_30_days():
    w = usage_windows(date(2026, 1, 1), date(2026, 3, 1), 30)
    assert w[0] == (date(2026, 1, 1), date(2026, 1, 30))
    assert w[-1][1] == date(2026, 3, 1)
    assert all((b - a).days < 30 for a, b in w)


def test_cumulative_from_base():
    h = {datetime(2026, 1, 1, 1, tzinfo=UTC): 1.0, datetime(2026, 1, 1, 0, tzinfo=UTC): 2.0}
    rows = cumulative(h, 10.0)
    assert [r[2] for r in rows] == [12.0, 13.0]


def test_five_minute_peaks_and_hourly_power() -> None:
    from datetime import UTC, date, datetime

    from custom_components.openfeed.usage import CONSUMPTION, EXPORT, summarise

    reads = [0.0] * 288
    reads[0] = 0.5  # 00:00-00:05 AEST, 6 kW
    reads[1] = 0.25  # 3 kW
    export = [0.0] * 288
    export[144] = -1.0  # 12:00 AEST, 12 kW export
    record = {"intervalDate": "2026-11-13", "reads": [
        {"registerSuffix": "E1", "readStartDate": "2026-11-13", "readUType": "intervalRead",
         "intervalRead": {"readIntervalLength": 5, "intervalReads": reads}},
        {"registerSuffix": "E2", "readStartDate": "2026-11-13", "readUType": "intervalRead",
         "intervalRead": {"readIntervalLength": 5, "intervalReads": [0.1] + [0.0] * 287}},
        {"registerSuffix": "B1", "readStartDate": "2026-11-13", "readUType": "intervalRead",
         "intervalRead": {"readIntervalLength": 5, "intervalReads": [abs(x) for x in export]}},
    ]}
    s = summarise([record])
    day = date(2026, 11, 13)
    # E1 + E2 in the first interval: 0.6 kWh over 5 minutes is 7.2 kW.
    assert s.daily_peak[CONSUMPTION][day][0] == 7.2
    assert s.daily_peak[CONSUMPTION][day][1] == datetime(2026, 11, 12, 14, 0, tzinfo=UTC)
    assert s.daily_peak[EXPORT][day][0] == 12.0
    mean, low, high = s.hourly_power[CONSUMPTION][datetime(2026, 11, 12, 14, tzinfo=UTC)]
    assert round(high, 3) == 7.2
    assert low == 0.0
    # (7.2 + 3) kW over twelve 5-minute slots.
    assert round(mean, 3) == round(10.2 / 12, 3)
    assert s.oldest_day == day


def test_sample_records_picks_good_and_empty_days_without_ids():
    records = [
        _record("2024-11-14", []),
        _record("2026-11-03", [0, 0, 0]),
        _record("2026-11-04", [0.5, 0]),
        _record("2026-11-05", [0.2]),
    ]
    out = sample_records(records)
    assert out["days"] == 4
    assert out["days_with_readings"] == 2 and out["days_without_readings"] == 2
    assert out["oldest_day_with_readings"] == "2026-11-04"
    assert out["newest_day_without_readings"] == "2026-11-03"
    assert out["data_day"]["intervalDate"] == "2026-11-05"
    assert out["empty_day"]["intervalDate"] == "2026-11-03"
    assert out["empty_day"]["reads"][0]["intervalRead"]["intervalReads"] == [0, 0, 0]
    assert "M1" not in str(out)
    assert out["data_day"]["meterId"] == "{id}"


def test_sample_records_empty():
    out = sample_records([])
    assert out["data_day"] is None and out["empty_day"] is None and out["days"] == 0


def test_uncovered_window_trims_covered_ends():
    from datetime import date, timedelta

    from custom_components.openfeed.usage import uncovered_window

    start, end = date(2026, 10, 1), date(2026, 10, 30)
    assert uncovered_window(start, end, set()) == (start, end)
    covered = {start + timedelta(days=i) for i in range(10)}
    assert uncovered_window(start, end, covered) == (date(2026, 10, 11), end)
    covered |= {end - timedelta(days=i) for i in range(5)}
    assert uncovered_window(start, end, covered) == (date(2026, 10, 11), date(2026, 10, 25))
    every = {start + timedelta(days=i) for i in range(30)}
    assert uncovered_window(start, end, every) is None
    # A gap in the middle keeps the window whole between covered ends.
    gap = every - {date(2026, 10, 15)}
    assert uncovered_window(start, end, gap) == (date(2026, 10, 15), date(2026, 10, 15))


def test_days_with_readings_ignores_empty_days():
    from datetime import date

    from custom_components.openfeed.usage import days_with_readings

    good = {"intervalDate": "2026-11-15", "reads": [{"intervalRead": {"intervalReads": [0, 0.1]}}]}
    empty = {"intervalDate": "2026-11-03", "reads": [{"intervalRead": {"intervalReads": None,
                                                                       "aggregateValue": 12.0}}]}
    zeros = {"intervalDate": "2026-11-02", "reads": [{"intervalRead": {"intervalReads": [0, 0]}}]}
    assert days_with_readings([good, empty, zeros, {"reads": []}]) == {date(2026, 11, 15)}
