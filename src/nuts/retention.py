"""Bounded, opt-in cleanup of NAS raw readings after aggregation.

Daily and monthly history is never deleted. The default command is read-only.
"""

import argparse
import os
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


REQUIRED_MONTH_VALUES = {
    "p1_reading": ("import_low_kwh", "import_high_kwh", "export_low_kwh",
                   "export_high_kwh", "gas_m3"),
    "water_event": ("water_m3",),
    "pv_reading": ("pv_generated_kwh",),
}
RAW_TABLES = tuple(REQUIRED_MONTH_VALUES)
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")


def cutoff_utc(now, retention_days, lookback_days):
    if retention_days < max(60, lookback_days + 2):
        raise ValueError("Ruwe bewaartermijn moet ten minste 60 dagen en langer dan de herberekening zijn")
    today = now.astimezone(LOCAL_TZ).date()
    local_midnight = datetime.combine(today - timedelta(days=retention_days),
                                      datetime.min.time(), LOCAL_TZ)
    return local_midnight.astimezone(timezone.utc).replace(tzinfo=None)


def one(connection, sql, params=()):
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        return cursor.fetchone()
    finally:
        cursor.close()


def oldest_candidate(connection, table, cutoff):
    return one(connection,
               "SELECT measured_at_utc FROM {} WHERE measured_at_utc < %s "
               "ORDER BY measured_at_utc LIMIT 1".format(table), (cutoff,))


def month_start_utc(month):
    return datetime.combine(month, datetime.min.time(), LOCAL_TZ).astimezone(
        timezone.utc).replace(tzinfo=None)


def archived_months(connection, table, first_utc, cutoff):
    """Only whole months with a stored value for this raw source may be removed."""
    columns = REQUIRED_MONTH_VALUES[table]
    first_local = first_utc.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).date()
    first_month = first_local.replace(day=1)
    cutoff_local = cutoff.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).date()
    cutoff_month = cutoff_local.replace(day=1)
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT usage_month, {} FROM monthly_usage "
            "WHERE usage_month >= %s AND usage_month < %s "
            "ORDER BY usage_month".format(", ".join(columns)),
            (first_month, cutoff_month),
        )
        return [row[0] for row in cursor.fetchall()
                if all(value is not None for value in row[1:])]
    finally:
        cursor.close()


def clean_table(connection, table, cutoff, max_rows, batch_size, apply):
    if table not in RAW_TABLES:
        raise ValueError("Onbekende ruwe tabel")
    first = oldest_candidate(connection, table, cutoff)
    if first is None:
        print("{}: geen oude rijen vóór {} UTC".format(table, cutoff), flush=True)
        return 0
    months = archived_months(connection, table, first[0], cutoff)
    if not apply:
        print("{}: oudste ruwe rij {} UTC; {} afgeronde maanden met bewaard totaal "
              "komen in aanmerking".format(table, first[0], len(months)), flush=True)
        return 0
    deleted = 0
    for month in months:
        next_month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
        lower, upper = month_start_utc(month), month_start_utc(next_month)
        while deleted < max_rows:
            take = min(batch_size, max_rows - deleted)
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "DELETE FROM {} WHERE measured_at_utc >= %s "
                    "AND measured_at_utc < %s ORDER BY measured_at_utc LIMIT %s".format(table),
                    (lower, upper, take),
                )
                count = cursor.rowcount
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                cursor.close()
            deleted += count
            if count < take:
                break
            time.sleep(0.05)
        if deleted >= max_rows:
            break
    print("{}: {} oude ruwe rijen verwijderd".format(table, deleted), flush=True)
    return deleted


def run_once(connection, now, retention_days, lookback_days, max_rows,
             batch_size, apply):
    cutoff = cutoff_utc(now, retention_days, lookback_days)
    if max_rows < 1 or batch_size < 1 or batch_size > max_rows:
        raise ValueError("Ongeldige batchgrootte of maximum")
    print("Ruwe metingen vóór {} UTC; dagelijkse en maandelijkse totalen blijven staan".format(
        cutoff), flush=True)
    total = 0
    for table in RAW_TABLES:
        total += clean_table(connection, table, cutoff, max_rows, batch_size, apply)
    return total


def main():
    parser = argparse.ArgumentParser(description="Controleer of ruim oude ruwe NAS-metingen op")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="voer de verwijdering uit")
    action.add_argument("--run", action="store_true", help="voer ieder kwartier begrensde opruiming uit")
    parser.add_argument("--max-rows", type=int, default=10000, help="maximum per tabel per ronde")
    parser.add_argument("--batch-size", type=int, default=500, help="rijen per transactie")
    args = parser.parse_args()

    import mysql.connector

    config = {
        "host": os.environ["DB_HOST"],
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
        "database": "nuts_v2",
        "connection_timeout": 5,
    }
    retention_days = int(os.getenv("RAW_RETENTION_DAYS", "90"))
    lookback_days = int(os.getenv("CALC_LOOKBACK_DAYS", "14"))
    while True:
        try:
            with mysql.connector.connect(**config) as connection:
                run_once(connection, datetime.now(timezone.utc), retention_days,
                         lookback_days, args.max_rows, args.batch_size,
                         args.apply or args.run)
        except Exception as exc:
            if not args.run:
                raise
            print("Opruiming wacht: {}".format(exc), flush=True)
        if not args.run:
            return
        time.sleep(900)


if __name__ == "__main__":
    main()
