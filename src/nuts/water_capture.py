"""Record water pulses immediately in SQLite; no NAS call in the GPIO path."""

import threading
import time
from typing import Callable, Optional

from .outbox import Outbox, utc_timestamp


class WaterCapture:
    def __init__(self, outbox: Outbox, lockout_seconds: float = 0.1,
                 clock: Callable[[], float] = time.monotonic):
        if outbox.kind != "water":
            raise ValueError("Een waterbuffer is vereist")
        if lockout_seconds < 0:
            raise ValueError("Negatieve flankblokkering")
        self.outbox = outbox
        self.lockout_seconds = lockout_seconds
        self.clock = clock
        self._last_accepted: Optional[float] = None
        self._lock = threading.Lock()

    def pulse(self) -> bool:
        """Persist a pulse and increment the counter in one transaction.

        Return False only for a contact bounce inside the lockout window.
        A storage failure propagates so the operator can see it; it never
        silently changes the local counter.
        """
        with self._lock:
            now = self.clock()
            if (self._last_accepted is not None and
                    now - self._last_accepted < self.lockout_seconds):
                return False
            self.outbox.add_water("pulse", utc_timestamp())
            self._last_accepted = now
            return True

    def heartbeat(self) -> None:
        self.outbox.add_water("heartbeat", utc_timestamp())
