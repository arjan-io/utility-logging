"""One-shot NAS test from a container, without opening P1 or GPIO devices."""

from datetime import datetime, timezone
import os
from uuid import uuid4

from .outbox import P1_COLUMNS, WATER_COLUMNS
from .sync import INSERT_P1, INSERT_WATER


def check_database(connection) -> None:
    """Exercise the runtime INSERTs and roll both rows back."""
    cursor = connection.cursor()
    p1_id, water_id = str(uuid4()), str(uuid4())
    measured_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    try:
        cursor.execute(
            "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA='nuts_v2' "
            "AND TABLE_NAME IN ('p1_reading', 'water_event')"
        )
        engines = dict(cursor.fetchall())
        if engines != {"p1_reading": "InnoDB", "water_event": "InnoDB"}:
            raise RuntimeError("P1- en watertabel moeten beide InnoDB zijn")

        # A metadata SELECT can start a transaction in Connector/Python.
        connection.rollback()
        connection.start_transaction()
        try:
            p1 = dict.fromkeys(P1_COLUMNS)
            p1.update({
                "event_id": p1_id,
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
            cursor.execute(INSERT_P1, tuple(p1[name] for name in P1_COLUMNS))

            water = {
                "event_id": water_id,
                "measured_at_utc": measured_at,
                "event_kind": "pulse",
                "liters": 1,
            }
            cursor.execute(INSERT_WATER, tuple(water[name] for name in WATER_COLUMNS))

            for table, event_id in (("p1_reading", p1_id), ("water_event", water_id)):
                cursor.execute(
                    "SELECT COUNT(*) FROM nuts_v2.{} WHERE event_id=%s".format(table),
                    (event_id,),
                )
                if cursor.fetchone()[0] != 1:
                    raise RuntimeError("Proefrij ontbreekt in {}".format(table))
        finally:
            connection.rollback()

        for table, event_id in (("p1_reading", p1_id), ("water_event", water_id)):
            cursor.execute(
                "SELECT COUNT(*) FROM nuts_v2.{} WHERE event_id=%s".format(table),
                (event_id,),
            )
            if cursor.fetchone()[0] != 0:
                raise RuntimeError("Rollback mislukt voor {}".format(table))
    finally:
        cursor.close()


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
        check_database(connection)
    finally:
        connection.close()
    print("OK: MariaDB bereikbaar; P1 en water schrijfbaar; proefrijen teruggedraaid")


if __name__ == "__main__":
    main()
