import unittest
import sqlite3
import os
from unittest.mock import patch
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from nuts.mqtt_topics import SUFFIXES, configured_topics, topic_targets
from nuts.publisher import (MeterRepository, build_payloads, costs_for_period,
                            live_home_power, publish_available, water_volume)
from nuts.usage import day_start_utc


UTC = timezone.utc


class FakeRepository:
    def __init__(self, now):
        today = now.astimezone(timezone(timedelta(hours=1))).date()
        self.yesterday_start = day_start_utc(today - timedelta(days=1))
        self.today_start = day_start_utc(today)
        self.now = now
        self.electric = {
            self.yesterday_start: (100, 200),
            self.today_start: (102, 203),
            now: (103, 204),
        }
        self.gas = {
            self.yesterday_start: 500,
            self.today_start: 501,
            now: Decimal("501.5"),
        }

    def variable_rates(self, commodity, day):
        if commodity == "electricity":
            return {"low": Decimal("0.20"), "high": Decimal("0.30")}
        if commodity == "gas":
            return {"low": Decimal("1.20"), "high": Decimal("1.20")}
        return {"low": Decimal("2"), "high": Decimal("2")}

    def electric_at(self, moment):
        values = self.electric.get(moment)
        return ({"measured_at_utc": moment.replace(tzinfo=None),
                 "import_low_kwh": values[0], "import_high_kwh": values[1]}
                if values else None)

    def gas_at(self, moment):
        value = self.gas.get(moment)
        return ({"gas_measured_at_utc": moment.replace(tzinfo=None),
                 "gas_m3": value} if value is not None else None)

    def water_events(self, start, end):
        rows = []
        moment = self.yesterday_start
        while moment <= self.now:
            liters = 10 if moment in (self.yesterday_start + timedelta(hours=12),
                                       self.today_start + timedelta(hours=6)) else 0
            rows.append({"measured_at_utc": moment.replace(tzinfo=None),
                         "liters": liters})
            moment += timedelta(minutes=5)
        return [row for row in rows if start - timedelta(minutes=15) <=
                row["measured_at_utc"].replace(tzinfo=UTC) <= end + timedelta(minutes=15)]

    def latest_p1(self):
        return {"measured_at_utc": self.now.replace(tzinfo=None),
                "import_w": 424, "export_w": 0,
                "phase_l1_import_w": 200, "phase_l2_import_w": 224,
                "phase_l3_import_w": 0, "phase_l3_export_w": 0,
                "voltage_l1_v": Decimal("230.1"),
                "voltage_l2_v": None, "voltage_l3_v": None,
                "current_l1_a": Decimal("1.2"),
                "current_l2_a": None, "current_l3_a": None}

    def latest_pv(self):
        return {"measured_at_utc": (self.now - timedelta(minutes=2)).replace(tzinfo=None),
                "current_w": 200}

    def gas_rate(self, now):
        return 0.5

    def water_flow(self, now):
        return 1.2


class PublisherTests(unittest.TestCase):
    def test_latest_pv_uses_power_without_cumulative_counter(self):
        class Connection:
            def cursor(self, dictionary=False):
                return self

            def execute(self, query, params):
                if "FROM pv_reading" in query:
                    self.result = {"measured_at_utc": datetime(2026, 11, 19, 2, 46),
                                   "current_w": 0}
                else:
                    self.result = {"measured_at_utc": datetime(2026, 11, 20, 12, 0),
                                   "current_w": 450}

            def fetchone(self):
                return self.result

            def close(self):
                pass

        self.assertEqual(MeterRepository(Connection()).latest_pv()["current_w"], 450)

    def test_final_mqtt_settings_publish_only_five_sensors_topics(self):
        with patch.dict(os.environ,
                        {"MQTT_PREFIX": "sensors", "MQTT_COMPAT_PREFIX": ""}):
            topics = [topic for suffix in SUFFIXES
                      for topic in configured_topics(suffix)]
        self.assertEqual(topics, [
            "sensors/dsmr/electricity", "sensors/dsmr/kosten",
            "sensors/dsmr/gas", "sensors/water", "sensors/pv",
        ])
        self.assertTrue(all("/v2/" not in topic for topic in topics))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(configured_topics("pv"), ("sensors/pv",))

    def test_house_power_uses_pv_import_and_export(self):
        now = datetime(2026, 11, 10, 12, tzinfo=UTC)
        p1 = {"measured_at_utc": now.replace(tzinfo=None),
              "import_w": 0, "export_w": 300}
        pv = {"measured_at_utc": (now - timedelta(minutes=2)).replace(tzinfo=None),
              "current_w": 850}
        self.assertEqual(live_home_power(p1, pv, now), (850, 550))
        p1.update(import_w=100, export_w=0)
        self.assertEqual(live_home_power(p1, pv, now), (850, 950))
        pv["current_w"] = 0
        self.assertEqual(live_home_power(p1, pv, now), (0, 100))
        p1["measured_at_utc"] = (now - timedelta(minutes=3)).replace(tzinfo=None)
        self.assertEqual(live_home_power(p1, pv, now), (0, None))
        pv["measured_at_utc"] = (now - timedelta(minutes=11)).replace(tzinfo=None)
        self.assertEqual(live_home_power(p1, pv, now), (None, None))

    def test_v2_and_existing_topics_stay_separate(self):
        self.assertEqual(topic_targets("dsmr/gas", "sensors/v2", "sensors"),
                         ("sensors/v2/dsmr/gas", "sensors/dsmr/gas"))
        self.assertEqual(topic_targets("water", "sensors/v2", "sensors"),
                         ("sensors/v2/water", "sensors/water"))
        self.assertEqual(topic_targets("dsmr/electricity", "sensors/v2", "sensors"),
                         ("sensors/v2/dsmr/electricity", "sensors/dsmr/electricity"))
        self.assertEqual(topic_targets("dsmr/gas", "sensors", "sensors"),
                         ("sensors/dsmr/gas",))

    def test_gas_boundary_uses_imported_reading_when_gas_time_is_missing(self):
        database = sqlite3.connect(":memory:")
        database.execute("""CREATE TABLE p1_reading (
            source TEXT, measured_at_utc TEXT, gas_measured_at_utc TEXT,
            gas_m3 NUMERIC)""")
        database.executemany("INSERT INTO p1_reading VALUES (?, ?, ?, ?)", [
            ("legacy_import", "2026-11-10 00:00:00", None, 100),
            ("collector", "2026-11-10 00:05:00", "2026-11-10 00:02:00", 101),
        ])

        class Connection:
            def cursor(self, dictionary=False):
                cursor = database.cursor()

                class Cursor:
                    def execute(self, query, params):
                        cursor.execute(query.replace("%s", "?"), params)

                    def fetchone(self):
                        values = cursor.fetchone()
                        if values is None:
                            return None
                        result = dict(zip((item[0] for item in cursor.description), values))
                        result["gas_measured_at_utc"] = datetime.fromisoformat(
                            result["gas_measured_at_utc"])
                        return result

                    def close(self):
                        cursor.close()

                return Cursor()

        repo = MeterRepository(Connection())
        self.assertEqual(repo.gas_at(datetime(2026, 11, 10, 0, 1, tzinfo=UTC))
                         ["gas_m3"], 100)
        self.assertEqual(repo.gas_at(datetime(2026, 11, 10, 0, 3, tzinfo=UTC))
                         ["gas_m3"], 101)
        database.close()

    def test_water_query_failure_does_not_hide_p1_and_gas(self):
        now = datetime(2026, 10, 31, 12, tzinfo=UTC)

        class BrokenWater(FakeRepository):
            def water_events(self, start, end):
                raise RuntimeError("water table unavailable")

        sent = {}
        errors = publish_available(BrokenWater(now),
                                   lambda topic, payload: sent.update({topic: payload}),
                                   now=now)
        self.assertEqual(set(sent), {"dsmr/electricity", "dsmr/kosten", "dsmr/gas"})
        self.assertEqual(set(errors), {"water"})

    def test_water_publishes_when_p1_device_has_no_readings(self):
        now = datetime(2026, 10, 31, 12, tzinfo=UTC)

        class NoP1(FakeRepository):
            def electric_at(self, moment):
                return None

            def gas_at(self, moment):
                return None

            def latest_p1(self):
                return None

        payloads = build_payloads(NoP1(now), now)
        self.assertIsNone(payloads["dsmr/electricity"])
        self.assertIsNone(payloads["dsmr/kosten"]["kostenVandaag"])
        self.assertEqual(payloads["water"]["costsToday"], 0.02)

    def test_costs_and_existing_payload_shapes(self):
        now = datetime(2026, 10, 31, 12, tzinfo=UTC)
        payloads = build_payloads(FakeRepository(now), now)
        self.assertEqual(payloads["dsmr/kosten"],
                         {"kostenVandaag": 0.5, "kostenGister": 1.3})
        self.assertEqual(payloads["dsmr/gas"],
                         {"rate": 0.5, "costsToday": 0.6,
                          "costsYesterday": 1.2})
        self.assertEqual(payloads["water"]["rate"], 1.2)
        self.assertEqual(payloads["water"]["costsToday"], 0.02)
        self.assertEqual(payloads["water"]["costsYesterday"], 0.02)
        self.assertEqual(payloads["dsmr/electricity"]["voltage"]["v1"], 230.1)
        self.assertEqual(payloads["dsmr/electricity"]["gebruik"]["huisW"], 624)
        self.assertEqual(payloads["dsmr/electricity"]["gebruik"]["pvW"], 200)

    def test_no_recent_water_event_has_unknown_rate(self):
        now = datetime(2026, 10, 31, 12, tzinfo=UTC)

        class NoRecentWater(FakeRepository):
            def water_flow(self, now):
                return None

        payload = build_payloads(NoRecentWater(now), now)["water"]
        self.assertIsNone(payload["rate"])

    def test_missing_boundary_does_not_become_zero_cost(self):
        now = datetime(2026, 10, 31, 12, tzinfo=UTC)
        repo = FakeRepository(now)
        repo.electric.pop(repo.today_start)
        day = date(2026, 10, 31)
        actual = costs_for_period(repo, "electricity", day, repo.today_start, now, False)
        self.assertIsNone(actual)

    def test_water_gap_is_unknown(self):
        start = datetime(2026, 10, 31, 0, tzinfo=UTC)
        end = start + timedelta(hours=1)
        rows = [{"measured_at_utc": moment.replace(tzinfo=None), "liters": 0}
                for moment in (start, start + timedelta(minutes=5),
                               start + timedelta(minutes=50), end)]
        self.assertIsNone(water_volume(rows, start, end, True))

    def test_old_hourly_water_zeros_are_estimated_coverage(self):
        start = datetime(2026, 10, 31, 0, tzinfo=UTC)
        end = start + timedelta(hours=2)
        rows = [{"measured_at_utc": (start + timedelta(hours=i)).replace(tzinfo=None),
                 "liters": 0, "source": "legacy_import"}
                for i in range(3)]
        rows.append({"measured_at_utc": (start + timedelta(minutes=30)).replace(tzinfo=None),
                     "liters": 1, "source": "legacy_import"})
        rows.sort(key=lambda row: row["measured_at_utc"])
        self.assertEqual(water_volume(rows, start, end, True), Decimal("0.001"))

    def test_water_cost_can_cross_legacy_to_collector_handoff(self):
        start = datetime(2026, 11, 10, 0, tzinfo=UTC)
        end = start + timedelta(hours=3)
        rows = [
            {"measured_at_utc": (start + timedelta(hours=i)).replace(tzinfo=None),
             "liters": 0, "source": "legacy_import"}
            for i in range(2)
        ]
        rows += [
            {"measured_at_utc": (start + timedelta(hours=2, minutes=i)).replace(tzinfo=None),
             "liters": 1 if i == 5 else 0, "source": "collector"}
            for i in range(0, 61, 5)
        ]
        self.assertEqual(water_volume(rows, start, end, True), Decimal("0.001"))


if __name__ == "__main__":
    unittest.main()
