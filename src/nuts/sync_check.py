"""One-shot SQLite-to-MariaDB check without permanent measurement rows."""

from datetime import datetime, timezone
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from .outbox import Outbox
from .sync import MariaDBSink, sync_batch


class RollbackOnlyConnection:
    """Let the normal sink execute its INSERTs without committing this probe."""

    def __init__(self, connection):
        self.connection = connection

    def cursor(self):
        return self.connection.cursor()

    def commit(self):
        pass

    def rollback(self):
        self.connection.rollback()


def check_sync_pipeline(connection) -> None:
    """Copy temporary outboxes, verify both rows, then roll them back."""
    measured_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    with TemporaryDirectory(prefix="nuts-sync-check-") as directory:
        with Outbox(str(Path(directory) / "p1.sqlite"), "p1") as p1, \
                Outbox(str(Path(directory) / "water.sqlite"), "water") as water:
            p1_id = p1.add_p1({
                "measured_at_utc": measured_at,
                "import_low_kwh": "1.000",
                "import_high_kwh": "1.000",
                "export_low_kwh": "0.000",
                "export_high_kwh": "0.000",
                "gas_m3": "1.000",
                "gas_measured_at_utc": measured_at,
                "import_w": 0,
                "export_w": 0,
            })
            water_id = water.add_water("pulse", measured_at)

            # These are disposable SQLite files. The actual MariaDB connection
            # stays in one uncommitted transaction throughout the probe.
            connection.rollback()
            connection.start_transaction()
            try:
                sink = MariaDBSink(RollbackOnlyConnection(connection))
                if sync_batch(p1, sink) != 1 or sync_batch(water, sink) != 1:
                    raise RuntimeError("Niet beide tijdelijke events gesynchroniseerd")
                if p1.pending_count() != 0 or water.pending_count() != 0:
                    raise RuntimeError("Tijdelijke SQLite-events niet bevestigd")
                cursor = connection.cursor()
                try:
                    for table, event_id in (("p1_reading", p1_id), ("water_event", water_id)):
                        cursor.execute(
                            "SELECT COUNT(*) FROM nuts_v2.{} WHERE event_id=%s".format(table),
                            (event_id,),
                        )
                        if cursor.fetchone()[0] != 1:
                            raise RuntimeError("Proefevent ontbreekt in {}".format(table))
                finally:
                    cursor.close()
            finally:
                connection.rollback()

            cursor = connection.cursor()
            try:
                for table, event_id in (("p1_reading", p1_id), ("water_event", water_id)):
                    cursor.execute(
                        "SELECT COUNT(*) FROM nuts_v2.{} WHERE event_id=%s".format(table),
                        (event_id,),
                    )
                    if cursor.fetchone()[0] != 0:
                        raise RuntimeError("Rollback mislukt voor {}".format(table))
            finally:
                cursor.close()
                connection.rollback()


def main() -> None:
    import mysql.connector

    connection = mysql.connector.connect(
        host=os.environ["DB_HOST"],
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database="nuts_v2",
        connection_timeout=5,
        autocommit=False,
    )
    try:
        check_sync_pipeline(connection)
    finally:
        connection.close()
    print("OK: tijdelijke P1- en waterbuffers gesynchroniseerd; MariaDB-rijen teruggedraaid")


if __name__ == "__main__":
    main()
