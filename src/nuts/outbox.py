"""Durable, Pi-local event buffers for P1 readings and water pulses.

The NAS is never touched on the acquisition path. Each SQLite connection
uses WAL and FULL synchronous mode; a committed event may be retried safely.
"""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading
from typing import Mapping, Optional
from uuid import uuid4


P1_COLUMNS = (
    "event_id", "measured_at_utc", "import_low_kwh", "import_high_kwh",
    "export_low_kwh", "export_high_kwh", "gas_m3", "gas_measured_at_utc",
    "import_w", "export_w", "phase_l1_import_w", "phase_l2_import_w",
    "phase_l3_import_w", "phase_l3_export_w",
)
WATER_COLUMNS = ("event_id", "measured_at_utc", "event_kind", "liters")
SCHEMAS = {
    "p1": "p1_outbox.sqlite.sql",
    "water": "water_outbox.sqlite.sql",
}


def utc_timestamp(moment: Optional[datetime] = None) -> str:
    """Return a UTC-naive SQL timestamp, with millisecond precision."""
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("Een tijdstip moet een tijdzone bevatten")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class Outbox:
    def __init__(self, path: str, kind: str):
        if kind not in SCHEMAS:
            raise ValueError("Onbekende buffer: {}".format(kind))
        self.kind = kind
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        table = self._table
        found = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not found:
            schema = Path(__file__).resolve().parents[2] / "sql" / SCHEMAS[kind]
            self.connection.executescript(schema.read_text(encoding="utf-8"))

    @property
    def _table(self) -> str:
        return "p1_outbox" if self.kind == "p1" else "water_outbox"

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def add_p1(self, reading: Mapping[str, object], event_id: Optional[str] = None) -> str:
        if self.kind != "p1":
            raise ValueError("Dit is geen P1-buffer")
        event_id = event_id or str(uuid4())
        values = {column: reading.get(column) for column in P1_COLUMNS}
        values["event_id"] = event_id
        for column in ("measured_at_utc", "import_low_kwh", "import_high_kwh",
                       "export_low_kwh", "export_high_kwh", "import_w", "export_w"):
            if values[column] is None:
                raise ValueError("P1-veld ontbreekt: {}".format(column))
        self._insert_or_verify(P1_COLUMNS, values)
        return event_id

    def add_water(self, kind: str, measured_at_utc: Optional[str] = None,
                  event_id: Optional[str] = None) -> str:
        if self.kind != "water":
            raise ValueError("Dit is geen waterbuffer")
        if kind not in ("pulse", "heartbeat"):
            raise ValueError("Ongeldig waterevent: {}".format(kind))
        values = {
            "event_id": event_id or str(uuid4()),
            "measured_at_utc": measured_at_utc or utc_timestamp(),
            "event_kind": kind,
            "liters": 1 if kind == "pulse" else 0,
        }
        self._insert_or_verify(WATER_COLUMNS, values)
        return values["event_id"]

    def _insert_or_verify(self, columns, values) -> None:
        table = self._table
        names = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        with self._lock:
            try:
                with self.connection:
                    self.connection.execute(
                        "INSERT INTO {} ({}) VALUES ({})".format(table, names, placeholders),
                        tuple(values[column] for column in columns),
                    )
            except sqlite3.IntegrityError:
                existing = self.connection.execute(
                    "SELECT {} FROM {} WHERE event_id=?".format(names, table),
                    (values["event_id"],),
                ).fetchone()
                if existing is None or any(
                    existing[column] != values[column] for column in columns
                ):
                    raise

    def pending(self, limit: int = 100):
        if limit < 1:
            raise ValueError("limit moet positief zijn")
        with self._lock:
            return [dict(row) for row in self.connection.execute(
                "SELECT * FROM {} WHERE delivered_at_utc IS NULL "
                "ORDER BY measured_at_utc, event_id LIMIT ?".format(self._table),
                (limit,),
            )]

    def mark_delivered(self, event_id: str) -> None:
        with self._lock:
            with self.connection:
                self.connection.execute(
                    "UPDATE {} SET delivered_at_utc=? "
                    "WHERE event_id=? AND delivered_at_utc IS NULL".format(self._table),
                    (utc_timestamp(), event_id),
                )

    def pending_count(self) -> int:
        with self._lock:
            return self.connection.execute(
                "SELECT COUNT(*) FROM {} WHERE delivered_at_utc IS NULL".format(self._table)
            ).fetchone()[0]

    def water_pulses_total(self) -> int:
        if self.kind != "water":
            raise ValueError("Dit is geen waterbuffer")
        with self._lock:
            return self.connection.execute(
                "SELECT pulses_total FROM water_counter WHERE id=1"
            ).fetchone()[0]
