import unittest
from datetime import date, timedelta

from nuts.aggregate_probe import preview
from nuts.usage import day_start_utc


class ProbeTests(unittest.TestCase):
    def test_real_day_preview_uses_only_selects(self):
        day = date(2026, 11, 6)
        start = day_start_utc(day)
        end = day_start_utc(day + timedelta(days=1))
        pv = [
            {"measured_at_utc": start.replace(tzinfo=None),
             "generated_total_kwh": "11875.000"},
            {"measured_at_utc": end.replace(tzinfo=None),
             "generated_total_kwh": "11878.600"},
        ]

        class Connection:
            def __init__(self):
                self.queries = []

            def cursor(self, dictionary=False):
                self.assert_dictionary = dictionary
                return self

            def execute(self, query, params):
                if not query.lstrip().upper().startswith("SELECT"):
                    raise AssertionError("Probe mag alleen SELECT uitvoeren")
                self.queries.append(query)
                self.result = pv if "FROM pv_reading" in query else []

            def fetchone(self):
                return None

            def fetchall(self):
                return self.result

            def close(self):
                pass

        connection = Connection()
        result = preview(connection, day)
        self.assertEqual(len(connection.queries), 6)
        self.assertEqual(result["source_rows"],
                         {"p1": 0, "water": 0, "pv": 2, "pv_day": 0})
        self.assertEqual(result["values"]["pv_generated_kwh"], "3.600")
        self.assertEqual(result["quality"]["pv"], "complete")
        self.assertIsNone(result["values"]["gas_m3"])

    def test_preview_shows_preserved_legacy_day(self):
        class Connection:
            def cursor(self, dictionary=False):
                return self

            def execute(self, query, params):
                self.query = query

            def fetchall(self):
                return []

            def fetchone(self):
                return {"electricity_quality": "unknown",
                        "gas_quality": "unknown", "water_quality": "unknown",
                        "pv_quality": "estimated", "pv_source": "legacy_import",
                        "pv_generated_kwh": "3.5"}

            def close(self):
                pass

        result = preview(Connection(), date(2026, 10, 1))
        self.assertEqual(result["values"]["pv_generated_kwh"], "3.5")
        self.assertEqual(result["preserved_existing"], ["pv"])
        self.assertEqual(result["source"]["pv"], "legacy_import")


if __name__ == "__main__":
    unittest.main()
