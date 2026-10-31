"""Single-owner P1 serial reader. Start only after the old reader stops."""

import os
import time

from .outbox import Outbox
from .p1_parser import parse_telegram


def frames(serial_port):
    """Yield bounded slash-to-CRC frames from a serial byte stream."""
    frame = bytearray()
    while True:
        line = serial_port.readline()
        if not line:
            continue
        if line.startswith(b"/"):
            frame = bytearray(line)
        elif frame:
            frame.extend(line)
        if len(frame) > 16384:
            frame.clear()
            continue
        if frame and line.startswith(b"!"):
            yield bytes(frame)
            frame.clear()


def run_forever() -> None:
    import serial

    device = os.environ["P1_DEVICE"]
    path = os.environ["P1_OUTBOX_PATH"]
    with Outbox(path, "p1") as outbox:
        while True:
            try:
                with serial.Serial(
                    port=device, baudrate=115200, bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                    xonxoff=True, timeout=20,
                ) as port:
                    for telegram in frames(port):
                        try:
                            reading = parse_telegram(telegram)
                        except ValueError as exc:
                            print("P1-telegram geweigerd: {}".format(exc), flush=True)
                            continue
                        outbox.add_p1(reading)
            except (serial.SerialException, OSError) as exc:
                print("P1-poort niet beschikbaar: {}".format(exc), flush=True)
                time.sleep(5)


if __name__ == "__main__":
    run_forever()
