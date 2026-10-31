import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from nuts.usage import cumulative_daily, day_start_utc, water_daily


UTC = timezone.utc


class UsageTests(unittest.TestCase):
    def test_25_hour_day_uses_local_midnights(self):
        day = date(2026, 10, 25)
        start = day_start_utc(day)
        end = day_start_utc(day + timedelta(days=1))
        self.assertEqual((end - start).total_seconds(), 25 * 3600)
        actual = cumulative_daily([(start, "100"), (end, "125")], day, day)
        self.assertEqual(actual[day], (Decimal("25.000"), "complete"))

    def test_long_outage_over_midnight_is_not_restart_day_usage(self):
        first = date(2026, 10, 31)
        second = first + timedelta(days=1)
        samples = [
            (day_start_utc(first), "0"),
            (datetime(2026, 10, 31, 22, 0, tzinfo=UTC), "10"),
            (datetime(2026, 11, 1, 4, 0, tzinfo=UTC), "20"),
            (day_start_utc(second + timedelta(days=1)), "30"),
        ]
        actual = cumulative_daily(samples, first, second)
        self.assertEqual(actual[first], (None, "unknown"))
        self.assertEqual(actual[second], (None, "unknown"))

    def test_short_midnight_crossing_is_estimated(self):
        day = date(2026, 10, 31)
        start = day_start_utc(day)
        midnight = day_start_utc(day + timedelta(days=1))
        end = day_start_utc(day + timedelta(days=2))
        actual = cumulative_daily([
            (start, "0"), (midnight - timedelta(minutes=1), "10"),
            (midnight + timedelta(minutes=1), "11"), (end, "16"),
        ], day, day + timedelta(days=1))
        self.assertEqual(actual[day], (Decimal("10.500"), "estimated"))
        self.assertEqual(actual[day + timedelta(days=1)], (Decimal("5.500"), "estimated"))

    def test_water_heartbeat_means_zero_is_known(self):
        day = date(2026, 10, 31)
        start = day_start_utc(day)
        events = [(start + timedelta(minutes=5 * index), 0) for index in range(289)]
        self.assertEqual(water_daily(events, day, day)[day],
                         (Decimal("0"), "complete"))
        events.append((start + timedelta(hours=12, minutes=1), 1))
        self.assertEqual(water_daily(events, day, day)[day],
                         (Decimal("0.001"), "complete"))
        interrupted = [event for event in events
                       if not (start + timedelta(hours=10) < event[0]
                               < start + timedelta(hours=11))]
        self.assertEqual(water_daily(interrupted, day, day)[day],
                         (None, "unknown"))


if __name__ == "__main__":
    unittest.main()
