"""Copy committed SQLite events to MariaDB, acknowledging only after commit."""

import os
import time
from typing import Mapping

from .outbox import Outbox, P1_COLUMNS, WATER_COLUMNS


INSERT_P1 = """
INSERT INTO nuts_v2.p1_reading
(event_id, source, measured_at_utc, received_at_utc,
 import_low_kwh, import_high_kwh, export_low_kwh, export_high_kwh,
 gas_m3, gas_measured_at_utc, import_w, export_w,
 phase_l1_import_w, phase_l2_import_w, phase_l3_import_w, phase_l3_export_w,
 voltage_l1_v, voltage_l2_v, voltage_l3_v,
 current_l1_a, current_l2_a, current_l3_a, meter_tariff_code)
VALUES (%s, 'collector', %s, UTC_TIMESTAMP(3), %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE event_id=VALUES(event_id)
"""
INSERT_WATER = """
INSERT INTO nuts_v2.water_event
(event_id, source, measured_at_utc, received_at_utc, event_kind, liters)
VALUES (%s, 'collector', %s, UTC_TIMESTAMP(3), %s, %s)
ON DUPLICATE KEY UPDATE event_id=VALUES(event_id)
"""


def sync_batch(outbox: Outbox, sink, limit: int = 100) -> int:
    """Store events in order. A failed store leaves that event pending."""
    count = 0
    for row in outbox.pending(limit):
        sink.store(outbox.kind, row)
        outbox.mark_delivered(row["event_id"])
        count += 1
    return count


class MariaDBSink:
    """One committed NAS transaction per event, making retries simple."""

    def __init__(self, connection):
        self.connection = connection

    def store(self, kind: str, row: Mapping[str, object]) -> None:
        if kind == "p1":
            statement, columns = INSERT_P1, P1_COLUMNS
        elif kind == "water":
            statement, columns = INSERT_WATER, WATER_COLUMNS
        else:
            raise ValueError(kind)
        cursor = self.connection.cursor()
        try:
            cursor.execute(statement, tuple(row[column] for column in columns))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()


def run_forever() -> None:
    """Run on the Pi after the schema and credentials are ready."""
    import mysql.connector

    db_config = {
        "host": os.environ["DB_HOST"],
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
        "database": "nuts_v2",
        "connection_timeout": 5,
    }
    kind = os.environ["OUTBOX_KIND"]
    if kind not in ("p1", "water"):
        raise ValueError("OUTBOX_KIND moet p1 of water zijn")
    path = os.environ["P1_OUTBOX_PATH" if kind == "p1" else "WATER_OUTBOX_PATH"]
    with Outbox(path, kind) as outbox:
        while True:
            try:
                connection = mysql.connector.connect(**db_config)
                try:
                    sink = MariaDBSink(connection)
                    synced = sync_batch(outbox, sink)
                finally:
                    connection.close()
                time.sleep(1 if synced else 5)
            except Exception as exc:
                print("Synchronisatie wacht op NAS: {}".format(exc), flush=True)
                time.sleep(10)


if __name__ == "__main__":
    run_forever()
