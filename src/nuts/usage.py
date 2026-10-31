"""Day totals from cumulative readings, with explicit outage quality."""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo


LOCAL = ZoneInfo("Europe/Amsterdam")
ZERO = Decimal("0")


def day_start_utc(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=LOCAL).astimezone(timezone.utc)


def days(first: date, last: date):
    current = first
    while current <= last:
        yield current
        current += timedelta(days=1)


def _seconds(span: timedelta) -> Decimal:
    return (Decimal(span.days * 86400 + span.seconds)
            + Decimal(span.microseconds) / Decimal("1000000"))


def cumulative_daily(
    samples: Iterable[Tuple[datetime, object]], first: date, last: date,
    max_boundary_gap_seconds: int = 120,
) -> Mapping[date, Tuple[Optional[Decimal], str]]:
    """Distribute meter deltas only when their day is identifiable.

    A short interval crossing local midnight is interpolated and marked
    estimated. A long interval crossing midnight makes its affected days
    unknown; its whole delta is never assigned to the restart day.
    """
    if first > last:
        raise ValueError("Ongeldig datumbereik")
    readings = sorted((moment, Decimal(str(value))) for moment, value in samples)
    if any(moment.tzinfo is None for moment, _ in readings):
        raise ValueError("Meetmoment moet tijdzone bevatten")
    totals = {day: ZERO for day in days(first, last)}
    estimated = set()
    unknown = set()
    for (start, old), (end, new) in zip(readings, readings[1:]):
        if end <= start:
            raise ValueError("Dubbele of omgekeerde meettijd")
        first_boundary = day_start_utc(start.astimezone(LOCAL).date() + timedelta(days=1))
        crosses_inside = first_boundary < end
        affected = []
        cursor = start
        while cursor < end:
            day = cursor.astimezone(LOCAL).date()
            boundary = day_start_utc(day + timedelta(days=1))
            piece_end = min(end, boundary)
            affected.append((day, cursor, piece_end))
            cursor = piece_end
        if new < old or (crosses_inside and _seconds(end - start) > max_boundary_gap_seconds):
            unknown.update(day for day, _, _ in affected)
            continue
        delta = new - old
        duration = _seconds(end - start)
        for day, piece_start, piece_end in affected:
            if day not in totals:
                continue
            totals[day] += delta * _seconds(piece_end - piece_start) / duration
            if crosses_inside:
                estimated.add(day)
    result = {}
    for day in totals:
        # A full day needs a measurement on each side of both boundaries.
        # This also detects a collector that started or stopped mid-day.
        covered = bool(readings and readings[0][0] <= day_start_utc(day)
                       and readings[-1][0] >= day_start_utc(day + timedelta(days=1)))
        if not covered or day in unknown:
            result[day] = (None, "unknown")
        else:
            quality = "estimated" if day in estimated else "complete"
            result[day] = (totals[day].quantize(Decimal("0.001")), quality)
    return result


def water_daily(
    events: Iterable[Tuple[datetime, object]], first: date, last: date,
    max_gap_seconds: int = 900,
) -> Mapping[date, Tuple[Optional[Decimal], str]]:
    """Sum pulses, using zero-heartbeats to detect periods without data."""
    if first > last:
        raise ValueError("Ongeldig datumbereik")
    rows = sorted((moment, Decimal(str(liters))) for moment, liters in events)
    if any(moment.tzinfo is None or liters < 0 for moment, liters in rows):
        raise ValueError("Ongeldig waterevent")
    totals = {day: ZERO for day in days(first, last)}
    for moment, liters in rows:
        day = moment.astimezone(LOCAL).date()
        if day in totals:
            totals[day] += liters
    result = {}
    for day, liters in totals.items():
        start, end = day_start_utc(day), day_start_utc(day + timedelta(days=1))
        anchors = [moment for moment, _ in rows if start - timedelta(seconds=max_gap_seconds) <= moment
                   <= end + timedelta(seconds=max_gap_seconds)]
        covered = bool(anchors and anchors[0] <= start and anchors[-1] >= end)
        if covered:
            covered = all(_seconds(right - left) <= max_gap_seconds
                          for left, right in zip(anchors, anchors[1:]))
        result[day] = (liters / Decimal("1000"), "complete") if covered else (None, "unknown")
    return result
