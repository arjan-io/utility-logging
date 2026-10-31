import tempfile
import unittest
from pathlib import Path

from nuts.outbox import Outbox
from nuts.p1_parser import crc16, parse_telegram
from nuts.sync import sync_batch
from nuts.water_capture import WaterCapture


WORKSPACE = Path(__file__).resolve().parents[1]


def telegram(lines):
    frame = ("/TEST\r\n" + "\r\n".join(lines) + "\r\n!").encode("ascii")
    return frame + "{:04X}".format(crc16(frame)).encode("ascii") + b"\r\n"


P1_LINES = [
    "0-0:1.0.0(261031020000W)",
    "1-0:1.8.1(001234.567*kWh)",
    "1-0:1.8.2(002345.678*kWh)",
    "1-0:2.8.1(000123.456*kWh)",
    "1-0:2.8.2(000234.567*kWh)",
    "1-0:1.7.0(00.424*kW)",
    "1-0:2.7.0(00.000*kW)",
    "0-1:24.2.1(261031015500W)(000987.654*m3)",
]


class CaptureTests(unittest.TestCase):
    def test_p1_precision_and_gas_time(self):
        reading = parse_telegram(telegram(P1_LINES))
        self.assertEqual(reading["import_low_kwh"], "1234.567")
        self.assertEqual(reading["import_w"], 424)
        self.assertEqual(reading["measured_at_utc"], "2026-10-31 01:00:00.000")
        self.assertEqual(reading["gas_measured_at_utc"], "2026-10-31 00:55:00.000")
        self.assertEqual(reading["gas_m3"], "987.654")

    def test_bad_crc_and_missing_register_are_rejected(self):
        frame = telegram(P1_LINES)
        with self.assertRaisesRegex(ValueError, "CRC klopt niet"):
            parse_telegram(frame.replace(b"001234.567", b"001234.568"))
        with self.assertRaisesRegex(ValueError, "export_high_kwh"):
            parse_telegram(telegram(P1_LINES[:4] + P1_LINES[5:]))

    def test_p1_outbox_keeps_events_until_ack(self):
        with tempfile.TemporaryDirectory(dir=str(WORKSPACE)) as temp:
            path = str(Path(temp) / "p1.sqlite")
            with Outbox(path, "p1") as outbox:
                event_id = outbox.add_p1(parse_telegram(telegram(P1_LINES)))
                self.assertEqual(outbox.pending_count(), 1)
            with Outbox(path, "p1") as reopened:
                self.assertEqual(reopened.pending_count(), 1)
                self.assertEqual(reopened.pending()[0]["event_id"], event_id)
                reopened.mark_delivered(event_id)
                self.assertEqual(reopened.pending_count(), 0)

    def test_water_counter_heartbeat_bounce_and_duplicate(self):
        current = [10.0]
        with tempfile.TemporaryDirectory(dir=str(WORKSPACE)) as temp:
            with Outbox(str(Path(temp) / "water.sqlite"), "water") as outbox:
                capture = WaterCapture(outbox, clock=lambda: current[0])
                self.assertTrue(capture.pulse())
                current[0] += 0.05
                self.assertFalse(capture.pulse())
                current[0] += 0.10
                self.assertTrue(capture.pulse())
                outbox.add_water("heartbeat")
                self.assertEqual(outbox.water_pulses_total(), 2)
                self.assertEqual(outbox.pending_count(), 3)
                identifier = "00000000-0000-0000-0000-000000000001"
                stamp = "2026-10-31 12:00:00.000"
                outbox.add_water("pulse", stamp, identifier)
                outbox.add_water("pulse", stamp, identifier)
                self.assertEqual(outbox.water_pulses_total(), 3)

    def test_failed_nas_and_retry_are_idempotent(self):
        class FakeSink:
            def __init__(self):
                self.rows = {}
                self.fail = True

            def store(self, kind, row):
                if self.fail:
                    self.fail = False
                    raise ConnectionError("NAS offline")
                self.rows[(kind, row["event_id"])] = row

        with tempfile.TemporaryDirectory(dir=str(WORKSPACE)) as temp:
            with Outbox(str(Path(temp) / "water.sqlite"), "water") as outbox:
                outbox.add_water("pulse")
                sink = FakeSink()
                with self.assertRaises(ConnectionError):
                    sync_batch(outbox, sink)
                self.assertEqual(outbox.pending_count(), 1)
                self.assertEqual(sync_batch(outbox, sink), 1)
                self.assertEqual(sync_batch(outbox, sink), 0)
                self.assertEqual(outbox.pending_count(), 0)
                self.assertEqual(len(sink.rows), 1)


if __name__ == "__main__":
    unittest.main()
