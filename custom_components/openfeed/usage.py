"""Turn openfeed interval meter reads into hourly and daily energy series.

openfeed mirrors the Consumer Data Standards meter read: each read covers a
register from midnight AEST (a fixed UTC+10 offset, no daylight saving) on
readStartDate, with interval values of readIntervalLength minutes.
Positive values are consumption and negative values are export.

The integration splits every register into three series:

* consumption  energy imported from the grid on general registers
* export       energy exported to the grid
* controlled_load  energy on controlled-load registers

This module has no Home Assistant imports so it can be unit tested alone.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

AEST = timezone(timedelta(hours=10), "AEST")

CONSUMPTION = "consumption"
EXPORT = "export"
CONTROLLED_LOAD = "controlled_load"
SERIES = (CONSUMPTION, EXPORT, CONTROLLED_LOAD)

_UNIT_SCALE = {"KWH": 1.0, "WH": 0.001, "MWH": 1000.0}


@dataclass(slots=True)
class UsageSummary:
    """Hourly and daily kWh per series, plus data-quality counts."""

    # series -> UTC hour start -> kWh
    hourly: dict[str, dict[datetime, float]] = field(default_factory=dict)
    # series -> AEST date -> kWh
    daily: dict[str, dict[date, float]] = field(default_factory=dict)
    # series -> UTC hour start -> (mean kW, min kW, max kW) across intervals
    hourly_power: dict[str, dict[datetime, tuple[float, float, float]]] = field(
        default_factory=dict
    )
    # series -> AEST date -> (peak kW, UTC start of the peak interval)
    daily_peak: dict[str, dict[date, tuple[float, datetime]]] = field(default_factory=dict)
    substituted_intervals: int = 0
    skipped_reads: int = 0
    interval_minutes: int | None = None

    @property
    def oldest_day(self) -> date | None:
        """The earliest AEST date with data in any series."""
        days = [d for s in self.daily.values() for d in s]
        return min(days) if days else None

    @property
    def latest_complete_day(self) -> date | None:
        """The latest AEST date with data in any series."""
        days = [d for s in self.daily.values() for d in s]
        return max(days) if days else None


def _register_key(read: dict[str, Any]) -> str:
    return "|".join(
        str(read.get(k) or "") for k in ("meterId", "registerId", "registerSuffix")
    )


def _series_for(read: dict[str, Any], value: float) -> tuple[str, float] | None:
    if value == 0:
        return None
    if read.get("controlledLoad"):
        return CONTROLLED_LOAD, abs(value)
    suffix = str(read.get("registerSuffix") or "").upper()
    # NEM12 suffixes starting with B are export registers. Some holders
    # report them as positive values, so trust the suffix over the sign.
    if suffix.startswith("B"):
        return EXPORT, abs(value)
    if value < 0:
        return EXPORT, -value
    return CONSUMPTION, value


def summarise(records: list[dict[str, Any]]) -> UsageSummary:
    """Aggregate EnergyUsage records into hourly and daily series.

    Duplicate intervals for the same register (overlapping pages or
    windows) are de-duplicated, the last value winning.
    """
    summary = UsageSummary()
    # (register, interval start UTC) -> (series, kWh)
    intervals: dict[tuple[str, datetime], tuple[str, float]] = {}

    for record in records:
        for read in record.get("reads") or []:
            if read.get("readUType") != "intervalRead" or not read.get("intervalRead"):
                summary.skipped_reads += 1
                continue
            scale = _UNIT_SCALE.get(str(read.get("unitOfMeasure") or "KWH").upper())
            start_text = read.get("readStartDate") or record.get("intervalDate")
            interval = read["intervalRead"]
            length = interval.get("readIntervalLength")
            if scale is None or not start_text or not length:
                summary.skipped_reads += 1
                continue
            start_day = date.fromisoformat(str(start_text)[:10])
            origin = datetime.combine(start_day, datetime.min.time(), AEST)
            summary.interval_minutes = int(length)
            for quality in interval.get("readQualities") or []:
                first = int(quality.get("startInterval", 1))
                last = int(quality.get("endInterval", first))
                summary.substituted_intervals += max(0, last - first + 1)
            register = _register_key(read)
            for index, raw in enumerate(interval.get("intervalReads") or []):
                if raw is None:
                    continue
                start = (origin + timedelta(minutes=index * int(length))).astimezone(UTC)
                classified = _series_for(read, float(raw) * scale)
                if classified is None:
                    intervals.pop((register, start), None)
                    continue
                intervals[(register, start)] = classified

    hourly: dict[str, dict[datetime, float]] = defaultdict(lambda: defaultdict(float))
    daily: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    # Site total per series and interval, summing registers (E1 + E2 and so on).
    per_interval: dict[str, dict[datetime, float]] = defaultdict(lambda: defaultdict(float))
    for (_register, start), (series, kwh) in intervals.items():
        hour = start.replace(minute=0, second=0, microsecond=0)
        hourly[series][hour] += kwh
        daily[series][start.astimezone(AEST).date()] += kwh
        per_interval[series][start] += kwh

    summary.hourly = {s: dict(sorted(v.items())) for s, v in hourly.items()}
    summary.daily = {s: dict(sorted(v.items())) for s, v in daily.items()}
    if summary.interval_minutes:
        summary.hourly_power, summary.daily_peak = _power(
            per_interval, summary.interval_minutes
        )
    return summary


def _power(
    per_interval: dict[str, dict[datetime, float]], minutes: int
) -> tuple[
    dict[str, dict[datetime, tuple[float, float, float]]],
    dict[str, dict[date, tuple[float, datetime]]],
]:
    """Average power per interval, rolled up to hourly mean/min/max and daily peaks.

    Intervals with no reading count as zero power, so an hour with one
    5-minute import spike still averages over the full hour.
    """
    per_hour = 60 // minutes if minutes and 60 % minutes == 0 else 1
    scale = 60 / minutes
    hourly: dict[str, dict[datetime, tuple[float, float, float]]] = {}
    peaks: dict[str, dict[date, tuple[float, datetime]]] = {}
    for series, values in per_interval.items():
        buckets: dict[datetime, list[float]] = defaultdict(list)
        day_peak: dict[date, tuple[float, datetime]] = {}
        for start, kwh in values.items():
            kw = kwh * scale
            buckets[start.replace(minute=0, second=0, microsecond=0)].append(kw)
            day = start.astimezone(AEST).date()
            if day not in day_peak or kw > day_peak[day][0]:
                day_peak[day] = (kw, start)
        rows = {}
        for hour, kws in sorted(buckets.items()):
            zeros = max(0, per_hour - len(kws))
            low = 0.0 if zeros else min(kws)
            rows[hour] = (sum(kws) / max(per_hour, len(kws)), low, max(kws))
        hourly[series] = rows
        peaks[series] = {d: (round(kw, 3), t) for d, (kw, t) in sorted(day_peak.items())}
    return hourly, peaks


def usage_windows(oldest: date, newest: date, window_days: int) -> list[tuple[date, date]]:
    """Split a date range into inclusive windows of at most window_days."""
    windows: list[tuple[date, date]] = []
    start = oldest
    while start <= newest:
        end = min(newest, start + timedelta(days=window_days - 1))
        windows.append((start, end))
        start = end + timedelta(days=1)
    return windows


def cumulative(hourly: dict[datetime, float], base: float) -> list[tuple[datetime, float, float]]:
    """(hour, state, running sum) rows for recorder statistics."""
    rows = []
    total = base
    for hour, kwh in sorted(hourly.items()):
        total += kwh
        rows.append((hour, kwh, total))
    return rows


_ID_KEYS = frozenset({"servicePointId", "meterId", "accountId", "nmi", "registerId"})


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("{id}" if k in _ID_KEYS and v else _scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def has_readings(record: dict[str, Any]) -> bool:
    """True if any read in the record carries a non-zero interval value."""
    for read in record.get("reads") or []:
        for raw in (read.get("intervalRead") or {}).get("intervalReads") or []:
            if raw not in (None, 0, 0.0, "0"):
                return True
    return False


def days_with_readings(records: list[dict[str, Any]]) -> set[date]:
    """The intervalDate of every record that carries interval readings."""
    days: set[date] = set()
    for record in records:
        text = str(record.get("intervalDate") or "")[:10]
        if text and has_readings(record):
            try:
                days.add(date.fromisoformat(text))
            except ValueError:
                continue
    return days


def uncovered_window(start: date, end: date, covered: set[date]) -> tuple[date, date] | None:
    """Shrink a request window past days already returned with readings.

    openfeed does not apply oldestDate, so one window can return far more
    than asked. Days a previous window already returned with readings are
    trimmed from both ends; None when nothing is left to ask for. A gap in
    the middle keeps the window whole, because the API has no way to skip it.
    """
    while start <= end and start in covered:
        start += timedelta(days=1)
    while end >= start and end in covered:
        end -= timedelta(days=1)
    return (start, end) if start <= end else None


def sample_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the newest day with readings and the newest day without any.

    Both records are returned raw apart from identifiers, so openfeed
    support can compare a good day with an empty one.
    """
    by_day: dict[str, dict[str, Any]] = {}
    for record in records:
        day = str(record.get("intervalDate") or "")[:10]
        if day:
            by_day[day] = record
    good = [d for d, r in by_day.items() if has_readings(r)]
    empty = [d for d, r in by_day.items() if not has_readings(r)]
    return {
        "days": len(by_day),
        "days_with_readings": len(good),
        "days_without_readings": len(empty),
        "oldest_day_with_readings": min(good) if good else None,
        "newest_day_without_readings": max(empty) if empty else None,
        "data_day": _scrub(by_day[max(good)]) if good else None,
        "empty_day": _scrub(by_day[max(empty)]) if empty else None,
    }
