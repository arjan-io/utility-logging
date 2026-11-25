import tempfile
import unittest
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from nuts.outbox import Outbox
from nuts.pv_capture import (capture_station_days, parse_station_reading,
                             parse_day_kwh, process_reading)
from nuts.sync import MariaDBSink, sync_batch


WORKSPACE = Path(__file__).resolve().parents[1]


class PvCaptureTests(unittest.TestCase):
    def test_unchanged_day_total_is_recorded_again_after_local_midnight(self):
        with tempfile.TemporaryDirectory(dir=str(WORKSPACE)) as directory:
            with Outbox(str(Path(directory) / "pv.sqlite"), "pv_day") as outbox:
                first = outbox.add_pv_day(
                    "2026-11-24", "2.700", measured_at_utc="2026-11-24 16:31:38.864")
                same_day = outbox.add_pv_day(
                    "2026-11-24", "2.700", measured_at_utc="2026-11-24 20:00:00.000")
                after_midnight = outbox.add_pv_day(
                    "2026-11-24", "2.700", measured_at_utc="2026-11-24 23:01:00.000")
                later = outbox.add_pv_day(
                    "2026-11-24", "2.700", measured_at_utc="2026-11-25 01:00:00.000")
                pending = outbox.pending()
        self.assertEqual(first, same_day)
        self.assertNotEqual(first, after_midnight)
        self.assertEqual(after_midnight, later)
        self.assertEqual(len(pending), 2)

    def test_unitless_total_requires_confirmed_unit(self):
        data = {"kpi": {"total_power": "11872.0"},
                "powerflow": {"pv": "0.0"}}
        with self.assertRaises(ValueError):
            parse_station_reading(data, "")
        self.assertEqual(parse_station_reading(data, "kWh"), ("11872.000", 0))

    def test_explicit_mwh_and_optional_power(self):
        data = {"kpi": {"total_power": "11.872 MWh"},
                "powerflow": {"pv": "1.2 kW"}}
        self.assertEqual(parse_station_reading(data, "kWh"), ("11872.000", 1200))
        data["powerflow"]["pv"] = "n/a"
        self.assertEqual(parse_station_reading(data, "kWh"), ("11872.000", None))

    def test_inverter_total_fallback_and_missing_total(self):
        data = {"kpi": {}, "inverter": {"invert_full": {"etotal": 11874.0}},
                "powerflow": {"pv": 852.0}}
        self.assertEqual(parse_station_reading(data, "kWh"), ("11874.000", 852))
        del data["inverter"]["invert_full"]["etotal"]
        with self.assertRaisesRegex(ValueError, "Meetwaarde ontbreekt"):
            parse_station_reading(data, "kWh")

    def test_zero_lifetime_counter_is_not_stored(self):
        with self.assertRaisesRegex(ValueError, "Cumulatieve PV-stand is nul"):
            parse_station_reading({"kpi": {"total_power": 0}}, "kWh")

    def test_historical_station_day_is_queued_with_local_date(self):
        class Station:
            def _web_client(self):
                return self

            def request(self, endpoint, method, payload):
                self.requested_day = payload["startTime"][:10]
                return {"proSystemTotalStats": "1.7"}

        station = Station()
        day = date(2026, 11, 19)
        with tempfile.TemporaryDirectory(dir=str(WORKSPACE)) as directory:
            with Outbox(str(Path(directory) / "pv.sqlite"), "pv_day") as outbox:
                current, completed = capture_station_days(
                    station, "private-id", ZoneInfo("Europe/Amsterdam"),
                    [day], outbox)
                outbox.add_pv_day("2026-11-19", current_w=0)
                pending = outbox.pending()
        self.assertIsNone(current)
        self.assertEqual(completed, {day})
        self.assertEqual(station.requested_day, "2026-11-19")
        self.assertEqual(len(pending), 2)
        self.assertTrue(all(row["usage_date"] == "2026-11-19" for row in pending))
        self.assertEqual({row["generated_kwh"] for row in pending}, {"1.700", None})
        self.assertEqual(next(row["current_w"] for row in pending
                              if row["generated_kwh"] is None), 0)

    def test_missing_historical_day_can_be_retried(self):
        class Station:
            def __init__(self):
                self.calls = 0

            def _web_client(self):
                return self

            def request(self, endpoint, method, payload):
                self.calls += 1
                return {} if self.calls == 1 else {"proSystemTotalStats": "1.7"}

        station = Station()
        day = date(2026, 11, 19)
        with tempfile.TemporaryDirectory(dir=str(WORKSPACE)) as directory:
            with Outbox(str(Path(directory) / "pv.sqlite"), "pv_day") as outbox:
                _, first_completed = capture_station_days(
                    station, "private-id", ZoneInfo("Europe/Amsterdam"),
                    [day], outbox)
                self.assertEqual(first_completed, set())
                self.assertEqual(outbox.pending_count(), 0)
                _, second_completed = capture_station_days(
                    station, "private-id", ZoneInfo("Europe/Amsterdam"),
                    [day], outbox)
                self.assertEqual(second_completed, {day})
                self.assertEqual(outbox.pending_count(), 1)

    def test_daily_buffer_retries_after_nas_failure(self):
        class Sink:
            def __init__(self):
                self.fail = True

            def store(self, kind, row):
                self.kind = kind
                if self.fail:
                    self.fail = False
                    raise OSError("NAS offline")

        with tempfile.TemporaryDirectory(dir=str(WORKSPACE)) as directory:
            with Outbox(str(Path(directory) / "pv.sqlite"), "pv_day") as outbox:
                outbox.add_pv_day("2026-11-19", "1.700")
                sink = Sink()
                with self.assertRaises(OSError):
                    sync_batch(outbox, sink)
                self.assertEqual(outbox.pending_count(), 1)
                self.assertEqual(sync_batch(outbox, sink), 1)
                self.assertEqual(sink.kind, "pv_day")
                self.assertEqual(outbox.pending_count(), 0)

    def test_day_generation_and_live_power_without_total(self):
        import json

        class OutboxStub:
            def add_pv(self, *args):
                raise AssertionError("No cumulative total available")

        class ClientStub:
            def is_connected(self):
                return True

            def publish(self, topic, payload, qos):
                self.topic = topic
                self.payload = json.loads(payload)
                self.qos = qos
                return type("Result", (), {"rc": 0})()

        client = ClientStub()
        data = {"kpi": {"power": "2.6"}, "powerflow": {"pv": "825.0"}}
        self.assertEqual(parse_day_kwh(data), 2.6)
        cache = process_reading(data, "kWh", "2026-11-05 11:58:56.000",
                                OutboxStub(), client, "sensors/v2/pv")
        self.assertEqual(cache, 2.6)
        self.assertEqual(client.topic, "sensors/v2/pv")
        self.assertEqual(client.payload["pac"], 825)
        self.assertEqual(client.payload["eDag"], 2.6)
        self.assertNotIn("pv", client.payload)
        self.assertEqual(client.payload["measuredAtUtc"], "2026-11-05T11:58:56.000Z")
        self.assertTrue(client.payload["dayGenerationFresh"])

    def test_live_power_without_total_is_buffered_separately(self):
        class OutboxStub:
            def add_pv(self, *args):
                raise AssertionError("No cumulative total available")

        class DailyStub:
            def add_pv_day(self, *args, **kwargs):
                self.args, self.kwargs = args, kwargs

        class ClientStub:
            def is_connected(self):
                return False

        daily = DailyStub()
        process_reading({"kpi": {}, "powerflow": {"pv": "450"}}, "kWh",
                        "2026-11-20 12:00:00.000", OutboxStub(), ClientStub(),
                        "sensors/pv", fallback_outbox=daily,
                        local_day=date(2026, 11, 20))
        self.assertEqual(daily.args, ("2026-11-20",))
        self.assertEqual(daily.kwargs["current_w"], 450)

    def test_compatibility_topic_receives_same_pv_payload(self):
        class OutboxStub:
            def add_pv(self, *args):
                pass

        class ClientStub:
            def __init__(self):
                self.messages = []

            def is_connected(self):
                return True

            def publish(self, topic, payload, qos):
                self.messages.append((topic, payload, qos))
                return type("Result", (), {"rc": 0})()

        client = ClientStub()
        data = {"kpi": {"total_power": "11874.6", "power": "2.6"},
                "powerflow": {"pv": "825.0"}}
        process_reading(data, "kWh", "2026-11-05 11:58:56.000",
                        OutboxStub(), client, "sensors/v2/pv",
                        compat_topic="sensors/pv")
        self.assertEqual([item[0] for item in client.messages],
                         ["sensors/v2/pv", "sensors/pv"])
        self.assertEqual(client.messages[0][1:], client.messages[1][1:])

    def test_mqtt_failure_keeps_pv_sqlite_write(self):
        class OutboxStub:
            def __init__(self):
                self.rows = []

            def add_pv(self, *args):
                self.rows.append(args)

        class ClientStub:
            def is_connected(self):
                return True

            def publish(self, *args, **kwargs):
                raise OSError("broker offline")

        outbox = OutboxStub()
        data = {"kpi": {"total_power": "11874.6"},
                "powerflow": {"pv": 825}}
        cache = process_reading(data, "kWh", "2026-11-05 11:58:56.000",
                                outbox, ClientStub(), "sensors/v2/pv", 2.6)
        self.assertEqual(cache, 2.6)
        self.assertEqual(outbox.rows, [("11874.600", 825,
                                        "2026-11-05 11:58:56.000")])

    def test_sqlite_retry_after_nas_failure(self):
        class Sink:
            def __init__(self):
                self.fail = True
                self.events = set()

            def store(self, kind, row):
                self.assert_kind(kind)
                if self.fail:
                    self.fail = False
                    raise OSError("NAS offline")
                self.events.add(row["event_id"])

            def assert_kind(self, kind):
                if kind != "pv":
                    raise AssertionError(kind)

        with tempfile.TemporaryDirectory(dir=str(WORKSPACE)) as directory:
            with Outbox(str(Path(directory) / "pv.sqlite"), "pv") as outbox:
                event_id = outbox.add_pv("11872.000", 0)
                sink = Sink()
                with self.assertRaises(OSError):
                    sync_batch(outbox, sink)
                self.assertEqual(outbox.pending_count(), 1)
                self.assertEqual(sync_batch(outbox, sink), 1)
                self.assertEqual(sink.events, {event_id})
                self.assertEqual(outbox.pending_count(), 0)

    def test_mariadb_insert_uses_pv_table(self):
        class Cursor:
            def execute(self, statement, params):
                self.statement = statement
                self.params = params

            def close(self):
                pass

        class Connection:
            def __init__(self):
                self.last = None
                self.commits = 0

            def cursor(self):
                self.last = Cursor()
                return self.last

            def commit(self):
                self.commits += 1

            def rollback(self):
                raise AssertionError("No rollback expected")

        db = Connection()
        MariaDBSink(db).store("pv", {
            "event_id": "test-id", "measured_at_utc": "2026-11-04 21:14:32.000",
            "generated_total_kwh": "11872.000", "current_w": 0,
        })
        self.assertIn("nuts_v2.pv_reading", db.last.statement)
        self.assertEqual(db.last.params[2], "11872.000")
        MariaDBSink(db).store("pv_day", {
            "event_id": "day-id", "measured_at_utc": "2026-11-20 20:55:42.000",
            "usage_date": "2026-11-19", "generated_kwh": "1.700",
            "current_w": None,
        })
        self.assertIn("nuts_v2.pv_day_observation", db.last.statement)
        self.assertEqual(db.last.params[3], "1.700")
        self.assertEqual(db.commits, 2)


if __name__ == "__main__":
    unittest.main()
