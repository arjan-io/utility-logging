"""Single-owner GPIO17 water reader. Start only after the old reader stops."""

import os
import threading
import time

from .outbox import Outbox
from .water_capture import WaterCapture


def run_forever() -> None:
    from gpiozero import Button
    from gpiozero.pins.lgpio import LGPIOFactory

    pin = int(os.getenv("WATER_GPIO_BCM", "17"))
    path = os.environ["WATER_OUTBOX_PATH"]
    failed = threading.Event()
    failure = []
    with Outbox(path, "water") as outbox:
        capture = WaterCapture(outbox)

        def on_pulse():
            try:
                capture.pulse()
            except Exception as exc:
                failure.append(exc)
                failed.set()

        # pull_up=True makes the falling edge (contact to ground) a press.
        # Our own 100 ms lockout ignores repeated edges; the input is not
        # required to stay low for 100 ms.
        factory = LGPIOFactory()
        button = Button(pin, pull_up=True, bounce_time=None, pin_factory=factory)
        try:
            button.when_pressed = on_pulse
            next_heartbeat = time.monotonic()
            while not failed.wait(1):
                if time.monotonic() >= next_heartbeat:
                    capture.heartbeat()
                    next_heartbeat = time.monotonic() + 300
        finally:
            button.close()
            factory.close()
        raise RuntimeError("Opslag van waterpuls mislukt") from failure[0]


if __name__ == "__main__":
    run_forever()
