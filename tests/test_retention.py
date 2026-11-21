import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from nuts.outbox import Outbox
from nuts.retention import clean_pv_power_only, clean_table, cutoff_utc


WORKSPACE = Path(__file__).resolve().parents[1]


class ReadOnlyCursor:
    def __init__(self, statements):
        self.statements = statements
        self.result = None

    def execute(self, statement, params=()):
        self.statements.append(statement)
        if "FROM water_event" in statement:
            self.result = (datetime(2024, 9, 5, 22, 25, 5),)
        elif "FROM monthly_usage" in statement:
            self.result = [(date(2024, 9, 1), 12)]
        else:
            raise AssertionError(statement)

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result

    def close(self):
        pass


class ReadOnlyConnection:
    def __init__(self):
        self.statements = []

    def cursor(self):
        return ReadOnlyCursor(self.statements)


class DeleteCursor(ReadOnlyCursor):
    rowcount = 0

    def execute(self, statement, params=()):
        if statement.startswith("DELETE"):
            self.statements.append((statement, params))
            self.rowcount = 2
        else:
            super().execute(statement, params)


class DeleteConnection(ReadOnlyConnection):
    def __init__(self):
        super().__init__()
        self.commits = 0

    def cursor(self):
        return DeleteCursor(self.statements)

    def commit(self):
        self.commits += 1

    def rollback(self):
        raise AssertionError("Geen rollback verwacht")


class RetentionTests(unittest.TestCase):
    def test_old_pv_power_cleanup_never_deletes_daily_totals(self):
        class Connection:
            rowcount = 2

            def __init__(self):
                self.queries = []
                self.commits = 0

            def cursor(self):
                return self

            def execute(self, statement, params):
                self.queries.append(statement)

            def fetchone(self):
                return (datetime(2026, 8, 1),)

            def close(self):
                pass

            def commit(self):
                self.commits += 1

            def rollback(self):
                raise AssertionError("Geen rollback verwacht")

        connection = Connection()
        cutoff = datetime(2026, 8, 12, 22)
        self.assertEqual(clean_pv_power_only(connection, cutoff, 10, 10, False), 0)
        self.assertTrue(all(query.startswith("SELECT") for query in connection.queries))
        self.assertEqual(clean_pv_power_only(connection, cutoff, 10, 10, True), 2)
        self.assertIn("generated_kwh IS NULL", connection.queries[-1])
        self.assertIn("LIMIT %s", connection.queries[-1])
        self.assertEqual(connection.commits, 1)

    def test_check_never_writes_to_nas(self):
        connection = ReadOnlyConnection()
        cutoff = datetime(2026, 8, 12, 22)
        self.assertEqual(clean_table(connection, "water_event", cutoff, 100, 10, False), 0)
        self.assertTrue(all(sql.startswith("SELECT") for sql in connection.statements))

    def test_local_day_cutoff_survives_dst_change(self):
        now = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
        self.assertEqual(cutoff_utc(now, 90, 14), datetime(2026, 8, 11, 22))
        with self.assertRaises(ValueError):
            cutoff_utc(now, 15, 14)

    def test_delete_is_limited_to_archived_local_month(self):
        connection = DeleteConnection()
        cutoff = datetime(2026, 8, 12, 22)
        self.assertEqual(clean_table(connection, "water_event", cutoff, 100, 10, True), 2)
        statement, params = next(item for item in connection.statements
                                 if isinstance(item, tuple))
        self.assertIn("LIMIT %s", statement)
        self.assertEqual(params, (datetime(2024, 8, 31, 22),
                                  datetime(2024, 9, 30, 22), 10))
        self.assertEqual(connection.commits, 1)

    def test_prune_keeps_pending_events_and_water_counter(self):
        now = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory(dir=str(WORKSPACE)) as temp:
            with Outbox(str(Path(temp) / "water.sqlite"), "water") as outbox:
                old_id = outbox.add_water("pulse", "2026-10-01 12:00:00.000")
                pending_id = outbox.add_water("pulse", "2026-10-01 12:01:00.000")
                outbox.mark_delivered(old_id)
                with outbox.connection:
                    outbox.connection.execute(
                        "UPDATE water_outbox SET delivered_at_utc=? WHERE event_id=?",
                        ("2026-11-01 12:00:00.000", old_id),
                    )
                self.assertEqual(outbox.prune_delivered(now=now), 1)
                self.assertEqual(outbox.pending()[0]["event_id"], pending_id)
                self.assertEqual(outbox.water_pulses_total(), 2)


if __name__ == "__main__":
    unittest.main()
