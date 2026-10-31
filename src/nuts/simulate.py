"""Offline smoke test: no Pi hardware, NAS, MQTT or external files."""

from pathlib import Path
from tempfile import TemporaryDirectory

from .outbox import Outbox
from .p1_parser import crc16, parse_telegram
from .sync import sync_batch
from .water_capture import WaterCapture


class FakeSink:
    def __init__(self):
        self.rows = {}
        self.offline = True

    def store(self, kind, row):
        if self.offline:
            raise ConnectionError("gesimuleerde NAS-storing")
        self.rows[(kind, row["event_id"])] = row


def sample_telegram():
    body = (
        "/SIMULATIE\r\n"
        "0-0:1.0.0(261031120000W)\r\n"
        "1-0:1.8.1(001234.567*kWh)\r\n"
        "1-0:1.8.2(002345.678*kWh)\r\n"
        "1-0:2.8.1(000123.456*kWh)\r\n"
        "1-0:2.8.2(000234.567*kWh)\r\n"
        "1-0:1.7.0(00.424*kW)\r\n"
        "1-0:2.7.0(00.000*kW)\r\n"
        "0-1:24.2.1(261031115500W)(000987.654*m3)\r\n!"
    ).encode("ascii")
    return body + "{:04X}".format(crc16(body)).encode("ascii") + b"\r\n"


def main():
    workspace = Path(__file__).resolve().parents[2]
    with TemporaryDirectory(dir=str(workspace)) as temp:
        with Outbox(str(Path(temp) / "p1.sqlite"), "p1") as p1, \
                Outbox(str(Path(temp) / "water.sqlite"), "water") as water:
            p1.add_p1(parse_telegram(sample_telegram()))
            capture = WaterCapture(water, lockout_seconds=0)
            capture.pulse()
            capture.pulse()
            capture.heartbeat()
            sink = FakeSink()
            try:
                sync_batch(p1, sink)
            except ConnectionError:
                pass
            assert p1.pending_count() == 1
            assert water.water_pulses_total() == 2
            sink.offline = False
            sync_batch(p1, sink)
            sync_batch(water, sink)
            assert p1.pending_count() == water.pending_count() == 0
            assert len(sink.rows) == 4
            print("OK: 1 P1-meting, 2 waterpulsen, 1 nulmeting")
            print("OK: NAS-storing hield events lokaal; herstart leverde alles eenmaal af")


if __name__ == "__main__":
    main()
