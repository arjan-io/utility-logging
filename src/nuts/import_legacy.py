"""Controleer of importeer oude ruwe metingen en periodegegevens in nuts_v2.

Standaard alleen een controle. Gebruik ``--apply`` pas na de SQL-backup.
Vereist op de uitvoerende machine ``mysql-connector-python`` en de
DB_HOST/DB_PORT/DB_USER/DB_PASSWORD omgevingsvariabelen.
Een apart alleen-lezen account gebruikt LEGACY_DB_USER/LEGACY_DB_PASSWORD.
"""

import argparse
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Mapping
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from .history import COMMODITIES, METRICS, build_legacy_usage, period_key


LOCAL = ZoneInfo("Europe/Amsterdam")


OLD_ENERGY = """
SELECT id, datum, glt, ght, llt, lht, gas
FROM `nuts-elekgas`.{table}
ORDER BY datum, id
"""
OLD_WATER = """
SELECT id, datum, liters, m3
FROM `nuts-water`.{table}
ORDER BY datum, id
"""
OLD_PV = """
SELECT id, datum, eDag, eMaand, eTotal
FROM `nuts-pv`.opbrengst
ORDER BY datum, id
"""
RAW_P1 = """
SELECT id, `time`, glt, ght, llt, lht, gas, hv, hl, l1, l2, l3, l3l
FROM `nuts-elekgas`.meetwaarden ORDER BY id
"""
RAW_WATER = """
SELECT id, datum, liter FROM `nuts-water`.liter ORDER BY datum, id
"""
RAW_P1_PAGE = """
SELECT id, `time`, glt, ght, llt, lht, gas, hv, hl, l1, l2, l3, l3l
FROM `nuts-elekgas`.meetwaarden WHERE id > %s ORDER BY id LIMIT %s
"""
RAW_WATER_PAGE = """
SELECT id, datum, liter FROM `nuts-water`.liter
WHERE id > %s ORDER BY id LIMIT %s
"""
INSERT_P1 = """
INSERT INTO nuts_v2.p1_reading
(event_id, source, measured_at_utc, received_at_utc,
 import_low_kwh, import_high_kwh, export_low_kwh, export_high_kwh,
 gas_m3, gas_measured_at_utc, import_w, export_w,
 phase_l1_import_w, phase_l2_import_w, phase_l3_import_w, phase_l3_export_w)
VALUES (%s, 'legacy_import', %s, NULL, %s, %s, %s, %s,
        %s, NULL, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE event_id=VALUES(event_id)
"""
INSERT_WATER = """
INSERT INTO nuts_v2.water_event
(event_id, source, measured_at_utc, received_at_utc, event_kind, liters)
VALUES (%s, 'legacy_import', %s, NULL, %s, %s)
ON DUPLICATE KEY UPDATE event_id=VALUES(event_id)
"""

METRIC_COMMODITY = {
    "import_low_kwh": "electricity",
    "import_high_kwh": "electricity",
    "export_low_kwh": "electricity",
    "export_high_kwh": "electricity",
    "gas_m3": "gas",
    "water_m3": "water",
    "pv_generated_kwh": "pv",
}


def read_rows(cursor, query: str) -> List[Mapping[str, object]]:
    cursor.execute(query)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, values)) for values in cursor.fetchall()]


def exclude_pv_rows(rows, excluded_ids):
    """Keep misdated source rows in MariaDB, but omit them from aggregates."""
    excluded_ids = set(excluded_ids)
    found = {row["id"] for row in rows if row["id"] in excluded_ids}
    missing = excluded_ids - found
    if missing:
        raise ValueError("PV-bron-ID's niet gevonden: {}".format(
            ", ".join(str(value) for value in sorted(missing))))
    return ([row for row in rows if row["id"] not in excluded_ids],
            [row for row in rows if row["id"] in excluded_ids])


def _event_id(table, legacy_id):
    return str(uuid5(NAMESPACE_URL, "nuts-v2/legacy/{}/{}".format(table, legacy_id)))


def _nonnegative(value, table, legacy_id, field, nullable=False):
    if value is None and nullable:
        return None
    if value is None:
        raise ValueError("{} id {}: {} ontbreekt".format(table, legacy_id, field))
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("{} id {}: {} is ongeldig".format(table, legacy_id, field))
    return number


def p1_values(row):
    table, legacy_id = "nuts-elekgas.meetwaarden", row["id"]
    if row["time"] is None:
        raise ValueError("{} id {}: tijd ontbreekt".format(table, legacy_id))
    counters = [_nonnegative(row[field], table, legacy_id, field)
                for field in ("glt", "ght", "llt", "lht")]
    gas = _nonnegative(row["gas"], table, legacy_id, "gas", nullable=True)
    powers = [_nonnegative(row[field], table, legacy_id, field, nullable=True)
              for field in ("hv", "hl", "l1", "l2", "l3", "l3l")]
    if powers[0] is None or powers[1] is None:
        raise ValueError("{} id {}: actueel vermogen ontbreekt".format(table, legacy_id))
    return (_event_id(table, legacy_id), row["time"], *counters, gas, *powers)


def water_values(row):
    table, legacy_id = "nuts-water.liter", row["id"]
    if row["datum"] is None:
        raise ValueError("{} id {}: tijd ontbreekt".format(table, legacy_id))
    liters = _nonnegative(row["liter"], table, legacy_id, "liter")
    return (_event_id(table, legacy_id), row["datum"],
            "pulse" if liters > 0 else "heartbeat", liters)


def raw_batches(connection, query, convert, size=1000):
    cursor = connection.cursor()
    try:
        cursor.execute(query)
        names = [column[0] for column in cursor.description]
        while True:
            rows = cursor.fetchmany(size)
            if not rows:
                break
            yield [convert(dict(zip(names, row))) for row in rows]
    finally:
        cursor.close()


def scan_raw(connection, query, convert):
    return sum(len(batch) for batch in raw_batches(connection, query, convert))


def analyze_water_batches(batches, max_gap_seconds=4500):
    """Use raw liters only for calendar days covered by hourly heartbeats."""
    totals = defaultdict(lambda: Decimal("0"))
    bad_days = set()
    previous = None
    first_day = last_day = None
    count = 0
    for batch in batches:
        for _, timestamp, _, liters in batch:
            stamp = timestamp.replace(tzinfo=timezone.utc)
            day = stamp.astimezone(LOCAL).date()
            if previous is not None:
                if stamp < previous:
                    raise ValueError("Oude waterrijen staan niet op tijdsvolgorde")
                if (stamp - previous).total_seconds() > max_gap_seconds:
                    gap_day = previous.astimezone(LOCAL).date()
                    while gap_day <= day:
                        bad_days.add(gap_day)
                        gap_day += timedelta(days=1)
            else:
                first_day = day
            last_day = day
            totals[day] += liters
            previous = stamp
            count += 1
    if first_day is None:
        return 0, [], []
    bad_days.update((first_day, last_day))
    complete = {day: liters for day, liters in totals.items()
                if day not in bad_days}
    daily = [{"id": "raw-{}".format(day), "datum": day,
              "liters": liters, "m3": liters / Decimal("1000")}
             for day, liters in sorted(complete.items())]
    monthly = []
    for month in sorted({day.replace(day=1) for day in totals}):
        next_month = month.replace(year=month.year + (month.month == 12),
                                   month=month.month % 12 + 1)
        days = [month + timedelta(days=offset)
                for offset in range((next_month - month).days)]
        if all(day in complete for day in days):
            liters = sum((complete[day] for day in days), Decimal("0"))
            monthly.append({"id": "raw-{}".format(month), "datum": month,
                            "liters": liters, "m3": liters / Decimal("1000")})
    return count, daily, monthly


def prefer_complete_raw_water(old_rows, derived_rows, grain):
    """Retain old totals where raw hourly coverage is incomplete."""
    indexed = {}
    for row in old_rows:
        key = period_key(row["datum"], grain)
        if key in indexed:
            raise ValueError("Dubbel oud waterrecord voor {}".format(key))
        indexed[key] = row
    differences = []
    for row in derived_rows:
        key = period_key(row["datum"], grain)
        if key in indexed and abs(Decimal(str(indexed[key]["liters"])) -
                                  Decimal(str(row["liters"]))) > Decimal("1"):
            differences.append(key)
        indexed[key] = row
    return [indexed[key] for key in sorted(indexed)], differences


def import_raw(source, destination, page_query, convert, statement, size=1000):
    """Import bounded ID pages; a restart safely repeats committed pages."""
    count = 0
    last_id = 0
    while True:
        source.ping(reconnect=True, attempts=3, delay=2)
        cursor = source.cursor()
        try:
            cursor.execute("SET time_zone = '+00:00'")
            cursor.execute(page_query, (last_id, size))
            names = [column[0] for column in cursor.description]
            source_rows = cursor.fetchall()
        finally:
            cursor.close()
        if not source_rows:
            break
        batch = [convert(dict(zip(names, row))) for row in source_rows]

        destination.ping(reconnect=True, attempts=3, delay=2)
        cursor = destination.cursor()
        try:
            cursor.execute("SET time_zone = '+00:00'")
            cursor.executemany(statement, batch)
            destination.commit()
        except Exception:
            destination.rollback()
            raise
        finally:
            cursor.close()
        count += len(batch)
        last_id = source_rows[-1][names.index("id")]
        if count % 10000 < len(batch):
            print("Ruwe import: {} rijen verwerkt tot bron-ID {}".format(
                count, last_id), flush=True)
    return count

def upsert_statement(grain: str) -> str:
    if grain not in ("day", "month"):
        raise ValueError(grain)
    table = "daily_usage" if grain == "day" else "monthly_usage"
    key = "usage_date" if grain == "day" else "usage_month"
    columns = [key] + list(METRICS)
    columns += [commodity + "_quality" for commodity in COMMODITIES]
    columns += [commodity + "_source" for commodity in COMMODITIES]
    columns.append("calculated_at_utc")
    placeholders = ", ".join(["%s"] * len(columns))

    updates = []
    for column in METRICS:
        source = METRIC_COMMODITY[column] + "_source"
        updates.append(
            "{0}=IF({1} IN ('legacy_import', 'not_available') "
            "AND VALUES({1})='legacy_import', VALUES({0}), {0})".format(
                column, source
            )
        )
    for commodity in COMMODITIES:
        quality = commodity + "_quality"
        source = commodity + "_source"
        updates.append(
            "{0}=IF({1} IN ('legacy_import', 'not_available') "
            "AND VALUES({1})='legacy_import', VALUES({0}), {0})".format(
                quality, source
            )
        )
    for commodity in COMMODITIES:
        source = commodity + "_source"
        updates.append(
            "{0}=IF({0} IN ('legacy_import', 'not_available') "
            "AND VALUES({0})='legacy_import', VALUES({0}), {0})".format(source)
        )
    updates.append("calculated_at_utc=VALUES(calculated_at_utc)")

    return (
        "INSERT INTO nuts_v2.{table} ({columns}) VALUES ({values}) "
        "ON DUPLICATE KEY UPDATE {updates}"
    ).format(
        table=table,
        columns=", ".join(columns),
        values=placeholders,
        updates=", ".join(updates),
    )


def row_values(bucket: Mapping[str, object], grain: str) -> tuple:
    key = "usage_date" if grain == "day" else "usage_month"
    columns = [key] + list(METRICS)
    columns += [commodity + "_quality" for commodity in COMMODITIES]
    columns += [commodity + "_source" for commodity in COMMODITIES]
    return tuple(bucket[column] for column in columns) + (datetime.utcnow(),)


def summary(name: str, buckets: List[Mapping[str, object]], grain: str) -> None:
    key = "usage_date" if grain == "day" else "usage_month"
    if not buckets:
        print("{}: geen rijen".format(name))
        return
    print(
        "{}: {} perioden, {} t/m {}".format(
            name, len(buckets), buckets[0][key], buckets[-1][key]
        )
    )
    for commodity in ("electricity", "gas", "water", "pv"):
        count = sum(
            row[commodity + "_source"] == "legacy_import" for row in buckets
        )
        print("  {}: {} perioden".format(commodity, count))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="schrijf na controle naar nuts_v2"
    )
    parser.add_argument("--pv-unit", choices=("kWh", "MWh"),
                        help="bevestigde eenheid van oude eDag en eMaand")
    parser.add_argument("--skip-pv-id", action="append", type=int, default=[],
                        help="sluit een aantoonbaar fout gedateerde oude PV-rij "
                             "uit van dag- en maandtotalen; herhaal per ID")
    args = parser.parse_args()
    if args.apply and not args.pv_unit:
        parser.error("Bevestig de oude PV-eenheid met --pv-unit vóór --apply")

    try:
        import mysql.connector
    except ImportError:
        parser.error("Installeer mysql-connector-python op de uitvoerende machine")

    target_config = {
        "host": os.getenv("DB_HOST", ""),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER", ""),
        "password": os.getenv("DB_PASSWORD", ""),
    }
    if not target_config["host"] or not target_config["user"]:
        parser.error("DB_HOST en DB_USER ontbreken")
    source_config = dict(target_config)
    source_config["user"] = os.getenv("LEGACY_DB_USER") or target_config["user"]
    source_config["password"] = (os.getenv("LEGACY_DB_PASSWORD")
                                 if os.getenv("LEGACY_DB_USER") else target_config["password"])

    source = mysql.connector.connect(**source_config)
    destination = None
    try:
        cursor = source.cursor()
        cursor.execute("SET time_zone = '+00:00'")
        pv_rows, skipped_pv = exclude_pv_rows(
            read_rows(cursor, OLD_PV), args.skip_pv_id)
        for row in skipped_pv:
            print("PV-bronrij id {} ({}) buiten dag- en maandtotalen gelaten; "
                  "oude tabel ongewijzigd".format(row["id"], row["datum"]))
        pv_factor = Decimal("1000") if args.pv_unit == "MWh" else Decimal("1")
        old_energy_months = read_rows(cursor, OLD_ENERGY.format(table="maanden"))
        old_energy_days = read_rows(cursor, OLD_ENERGY.format(table="dagen"))
        old_water_months = read_rows(cursor, OLD_WATER.format(table="maandenwater"))
        old_water_days = read_rows(cursor, OLD_WATER.format(table="dagenwater"))
        cursor.close()

        water_count, raw_water_days, raw_water_months = analyze_water_batches(
            raw_batches(source, RAW_WATER, water_values))
        water_months, month_differences = prefer_complete_raw_water(
            old_water_months, raw_water_months, "month")
        water_days, day_differences = prefer_complete_raw_water(
            old_water_days, raw_water_days, "day")
        months, month_warnings = build_legacy_usage(
            old_energy_months,
            water_months,
            "month",
            pv_rows,
            pv_factor,
        )
        days, day_warnings = build_legacy_usage(
            old_energy_days,
            water_days,
            "day",
            pv_rows,
            pv_factor,
        )
        summary("Maanden", months, "month")
        summary("Dagen", days, "day")
        print("Water uit ruwe pulsen: {} volledige dagen, {} volledige maanden; "
              "{} afwijkende oude dagen, {} afwijkende oude maanden".format(
                  len(raw_water_days), len(raw_water_months),
                  len(day_differences), len(month_differences)))
        for period in (day_differences[:5] + month_differences[:5]):
            print("  Oude waterwaarde wijkt af van ruwe pulsen: {}".format(period))
        warnings = month_warnings + day_warnings
        for warning in warnings:
            print("WAARSCHUWING: {}".format(warning))

        p1_count = scan_raw(source, RAW_P1, p1_values)
        print("Ruwe gegevens: {} P1-rijen, {} waterrijen".format(
            p1_count, water_count))

        if not args.apply:
            if pv_rows and not args.pv_unit:
                print("Oude PV-eenheid nog niet bevestigd; proef gebruikt voorlopig kWh.")
            print("Controlemodus: niets geschreven. Gebruik --apply pas na backup.")
            return 0
        if warnings:
            print(
                "{} bronafwijkingen gemeld. Ongeldige of dubbele periodewaarden "
                "blijven NULL met kwaliteit unknown; overige maandstanden zijn "
                "schattingen.".format(len(warnings))
            )

        destination = mysql.connector.connect(**target_config)
        dest_cursor = destination.cursor()
        dest_cursor.execute("SET time_zone = '+00:00'")
        dest_cursor.close()
        p1_imported = import_raw(source, destination, RAW_P1_PAGE, p1_values, INSERT_P1)
        water_imported = import_raw(source, destination, RAW_WATER_PAGE,
                                    water_values, INSERT_WATER)
        dest_cursor = destination.cursor()
        for grain, buckets in (("month", months), ("day", days)):
            statement = upsert_statement(grain)
            dest_cursor.executemany(
                statement, [row_values(bucket, grain) for bucket in buckets]
            )
        destination.commit()
        dest_cursor.close()
        print("Import afgerond: {} P1, {} water, {} maanden, {} dagen.".format(
            p1_imported, water_imported, len(months), len(days)))
        return 0
    except Exception:
        if destination is not None:
            destination.rollback()
        raise
    finally:
        source.close()
        if destination is not None:
            destination.close()


if __name__ == "__main__":
    raise SystemExit(main())
