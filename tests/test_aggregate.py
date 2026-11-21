import unittest
from datetime import date, timedelta
from decimal import Decimal

from nuts.aggregate import _merge_existing, build_daily, build_monthly
from nuts.usage import day_start_utc


class AggregateTests(unittest.TestCase):
    def test_pv_five_minute_poll_crosses_midnight(self):
        day = date(2026, 11, 4)
        start = day_start_utc(day)
        pv = []
        for index in range(290):
            moment = start - timedelta(minutes=2) + timedelta(minutes=5 * index)
            pv.append({"measured_at_utc": moment.replace(tzinfo=None),
                       "generated_total_kwh": "11872.000" if index < 145 else "11875.600"})
        actual = build_daily(day, [], [], pv)
        self.assertEqual(actual["pv_generated_kwh"], Decimal("3.600"))
        self.assertEqual(actual["pv_quality"], "complete")

    def test_pv_zero_counter_is_ignored_at_midnight(self):
        day = date(2026, 11, 17)
        start, end = day_start_utc(day), day_start_utc(day + timedelta(days=1))
        pv = [
            (start - timedelta(minutes=1), "11902.600"),
            (start + timedelta(minutes=1), "0.000"),
            (start + timedelta(minutes=2), "11902.600"),
            (start + timedelta(hours=12), "11903.500"),
            (end - timedelta(minutes=1), "11903.500"),
            (end + timedelta(minutes=1), "11903.500"),
        ]
        rows = [{"measured_at_utc": moment.replace(tzinfo=None),
                 "generated_total_kwh": value} for moment, value in pv]
        actual = build_daily(day, [], [], rows,
                             [{"generated_kwh": "1.200"}])
        self.assertEqual(actual["pv_generated_kwh"], Decimal("0.900"))
        self.assertEqual(actual["pv_quality"], "complete")

    def test_pv_daylight_only_uses_consecutive_day_end_stands(self):
        day = date(2026, 11, 18)
        prior = day_start_utc(day - timedelta(days=1))
        start = day_start_utc(day)
        pv = [
            (prior + timedelta(hours=15), "11903.500"),
            (start + timedelta(hours=7), "11903.500"),
            (start + timedelta(hours=15), "11904.900"),
            (start + timedelta(hours=17), "0.000"),
        ]
        rows = [{"measured_at_utc": moment.replace(tzinfo=None),
                 "generated_total_kwh": value} for moment, value in pv]
        actual = build_daily(day, [], [], rows)
        self.assertEqual(actual["pv_generated_kwh"], Decimal("1.400"))
        self.assertEqual(actual["pv_quality"], "estimated")

    def test_pv_stale_nighttime_stand_is_not_a_day_total(self):
        day = date(2026, 11, 19)
        prior = day_start_utc(day - timedelta(days=1))
        start = day_start_utc(day)
        pv = [
            (prior + timedelta(hours=15), "11904.900"),
            (start + timedelta(hours=2), "11904.900"),
            (start + timedelta(hours=3), "0.000"),
        ]
        rows = [{"measured_at_utc": moment.replace(tzinfo=None),
                 "generated_total_kwh": value} for moment, value in pv]
        actual = build_daily(day, [], [], rows)
        self.assertIsNone(actual["pv_generated_kwh"])
        self.assertEqual(actual["pv_quality"], "unknown")

    def test_goodwe_daily_restores_day_without_lifetime_counter(self):
        day = date(2026, 11, 19)
        start = day_start_utc(day)
        pv = [{"measured_at_utc": (start + timedelta(hours=2)).replace(tzinfo=None),
               "generated_total_kwh": "11904.900"}]
        daily = [{"measured_at_utc": (start + timedelta(hours=24)).replace(tzinfo=None),
                  "generated_kwh": "1.700"}]
        actual = build_daily(day, [], [], pv, daily)
        self.assertEqual(actual["pv_generated_kwh"], Decimal("1.700"))
        self.assertEqual(actual["pv_quality"], "estimated")
        self.assertEqual(actual["pv_source"], "goodwe_daily")

    def test_water_day_survives_missing_p1(self):
        day = date(2026, 10, 31)
        start = day_start_utc(day)
        water = [{"measured_at_utc": (start + timedelta(minutes=5 * i)).replace(tzinfo=None),
                  "liters": 1 if i == 12 else 0}
                 for i in range(289)]
        actual = build_daily(day, [], water, [])
        self.assertIsNone(actual["import_low_kwh"])
        self.assertEqual(actual["electricity_quality"], "unknown")
        self.assertEqual(actual["water_m3"], Decimal("0.001"))
        self.assertEqual(actual["water_quality"], "complete")

    def test_closed_day_uses_precise_stands_and_water_pulses(self):
        day = date(2026, 10, 31)
        start, end = day_start_utc(day), day_start_utc(day + timedelta(days=1))
        p1 = []
        for moment, change in ((start, 0), (end, 1)):
            p1.append({"measured_at_utc": moment.replace(tzinfo=None),
                       "gas_measured_at_utc": moment.replace(tzinfo=None),
                       "import_low_kwh": Decimal("100.125") + change,
                       "import_high_kwh": Decimal("200.125") + change,
                       "export_low_kwh": Decimal("10.125") + change,
                       "export_high_kwh": Decimal("20.125") + change,
                       "gas_m3": Decimal("500.250") + change})
        water = [{"measured_at_utc": (start + timedelta(minutes=5 * i)).replace(tzinfo=None),
                  "liters": 1 if i == 12 else 0}
                 for i in range(289)]
        actual = build_daily(day, p1, water, [])
        self.assertEqual(actual["import_low_kwh"], Decimal("1.000"))
        self.assertEqual(actual["gas_m3"], Decimal("1.000"))
        self.assertEqual(actual["water_m3"], Decimal("0.001"))
        self.assertEqual(actual["electricity_quality"], "complete")
        self.assertEqual(actual["water_quality"], "complete")
        self.assertEqual(actual["pv_quality"], "not_collected")

    def test_legacy_month_is_kept_when_new_days_missing(self):
        month = date(2026, 10, 1)
        proposed = build_monthly(month, [], date(2026, 11, 1))
        existing = proposed.copy()
        existing.update({"gas_m3": Decimal("42"),
                         "gas_quality": "estimated",
                         "gas_source": "legacy_import"})
        merged = _merge_existing(proposed, existing)
        self.assertEqual(merged["gas_m3"], Decimal("42"))
        self.assertEqual(merged["gas_source"], "legacy_import")

    def test_complete_pv_is_kept_over_a_later_estimate(self):
        day = date(2026, 11, 17)
        proposed = build_daily(day, [], [], [])
        proposed.update({"pv_generated_kwh": Decimal("0.8"),
                         "pv_quality": "estimated", "pv_source": "meter_derived"})
        existing = proposed.copy()
        existing.update({"pv_generated_kwh": Decimal("0.9"),
                         "pv_quality": "complete"})
        merged = _merge_existing(proposed, existing)
        self.assertEqual(merged["pv_generated_kwh"], Decimal("0.9"))
        self.assertEqual(merged["pv_quality"], "complete")

    def test_full_month_is_summed(self):
        month = date(2026, 10, 1)
        rows = []
        for day in range(1, 32):
            row = {"usage_date": date(2026, 10, day),
                   "import_low_kwh": Decimal("1"),
                   "import_high_kwh": Decimal("2"),
                   "export_low_kwh": Decimal("0"),
                   "export_high_kwh": Decimal("0"),
                   "gas_m3": Decimal("0.5"),
                   "water_m3": Decimal("0.01"),
                   "pv_generated_kwh": None}
            for commodity in ("electricity", "gas", "water"):
                row[commodity + "_quality"] = "complete"
            row["pv_quality"] = "not_collected"
            rows.append(row)
        result = build_monthly(month, rows, date(2026, 11, 1))
        self.assertEqual(result["import_low_kwh"], Decimal("31"))
        self.assertEqual(result["gas_m3"], Decimal("15.5"))
        self.assertEqual(result["water_m3"], Decimal("0.31"))
        self.assertEqual(result["electricity_source"], "daily_sum")


if __name__ == "__main__":
    unittest.main()
