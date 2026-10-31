"""Rebuild closed local days and months from raw events on the NAS.

Re-running is safe. A known legacy or meter-derived value is never replaced
by an unknown value caused by a temporary missing raw-data window.
"""

import os
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from .usage import cumulative_daily, day_start_utc, water_daily


METRICS = (
    "import_low_kwh", "import_high_kwh", "export_low_kwh",
    "export_high_kwh", "gas_m3", "water_m3", "pv_generated_kwh",
)
COMMODITY_METRICS = {
    "electricity": METRICS[:4],
    "gas": ("gas_m3",),
    "water": ("water_m3",),
    "pv": ("pv_generated_kwh",),
}
COMMODITIES = tuple(COMMODITY_METRICS)


def aware(moment):
    return moment.replace(tzinfo=timezone.utc)


def sql_time(moment):
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def rows(connection, statement, params):
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(statement, params)
        return cursor.fetchall()
    finally:
        cursor.close()


def one(connection, statement, params):
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(statement, params)
        return cursor.fetchone()
    finally:
        cursor.close()


def _quality(qualities):
    if "unknown" in qualities:
        return "unknown"
    if "estimated" in qualities:
        return "estimated"
    return "complete"


def build_daily(day, p1_rows, water_rows, pv_rows):
    result = {"usage_date": day}
    for metric in METRICS:
        result[metric] = None
    for commodity in COMMODITIES:
        result[commodity + "_quality"] = "unknown" if commodity != "pv" else "not_collected"
        result[commodity + "_source"] = "not_available"

    electricity_qualities = []
    for metric in COMMODITY_METRICS["electricity"]:
        samples = [(aware(row["measured_at_utc"]), row[metric])
                   for row in p1_rows if row[metric] is not None]
        value, quality = cumulative_daily(samples, day, day)[day]
        result[metric] = value
        electricity_qualities.append(quality)
    if p1_rows:
        result["electricity_quality"] = _quality(electricity_qualities)
        result["electricity_source"] = "meter_derived"

    gas_samples = {}
    for row in p1_rows:
        if row["gas_measured_at_utc"] is not None and row["gas_m3"] is not None:
            stamp = aware(row["gas_measured_at_utc"])
            value = Decimal(str(row["gas_m3"]))
            if stamp in gas_samples and gas_samples[stamp] != value:
                raise ValueError("Tegenstrijdige gasstanden op {}".format(stamp))
            gas_samples[stamp] = value
    if gas_samples:
        value, quality = cumulative_daily(gas_samples.items(), day, day, 900)[day]
        result["gas_m3"] = value
        result["gas_quality"] = quality
        result["gas_source"] = "meter_derived"

    if water_rows:
        events = [(aware(row["measured_at_utc"]), row["liters"])
                  for row in water_rows]
        legacy_water = any(row.get("source") == "legacy_import" for row in water_rows)
        value, quality = water_daily(events, day, day,
                                     4500 if legacy_water else 900)[day]
        result["water_m3"] = value
        result["water_quality"] = "estimated" if legacy_water and quality == "complete" else quality
        result["water_source"] = "meter_derived"

    if pv_rows:
        samples = [(aware(row["measured_at_utc"]), row["generated_total_kwh"])
                   for row in pv_rows]
        value, quality = cumulative_daily(samples, day, day)[day]
        result["pv_generated_kwh"] = value
        result["pv_quality"] = quality
        result["pv_source"] = "meter_derived"
    return result


def _merge_existing(proposed, existing):
    if existing is None:
        return proposed
    for commodity, metrics in COMMODITY_METRICS.items():
        quality = commodity + "_quality"
        source = commodity + "_source"
        if (proposed[quality] in ("unknown", "not_collected") and
                existing[quality] in ("complete", "estimated")):
            for metric in metrics:
                proposed[metric] = existing[metric]
            proposed[quality] = existing[quality]
            proposed[source] = existing[source]
    return proposed


def _upsert(connection, table, key, values):
    columns = list(values) + ["calculated_at_utc"]
    names = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(values) + ["UTC_TIMESTAMP(3)"])
    updates = ", ".join("{0}=VALUES({0})".format(column)
                        for column in columns if column != key)
    statement = "INSERT INTO {0} ({1}) VALUES ({2}) ON DUPLICATE KEY UPDATE {3}".format(
        table, names, placeholders, updates)
    cursor = connection.cursor()
    try:
        cursor.execute(statement, tuple(values.values()))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def recalculate_day(connection, day):
    start, end = day_start_utc(day), day_start_utc(day + timedelta(days=1))
    margin = timedelta(minutes=20)
    window = (sql_time(start - margin), sql_time(end + margin))
    p1 = rows(connection, """
        SELECT measured_at_utc, import_low_kwh, import_high_kwh,
               export_low_kwh, export_high_kwh, gas_m3, gas_measured_at_utc
        FROM p1_reading WHERE measured_at_utc >= %s AND measured_at_utc <= %s
        ORDER BY measured_at_utc
    """, window)
    water = rows(connection, """
        SELECT measured_at_utc, liters, source FROM water_event
        WHERE measured_at_utc >= %s AND measured_at_utc <= %s
        ORDER BY measured_at_utc
    """, window)
    pv = rows(connection, """
        SELECT measured_at_utc, generated_total_kwh FROM pv_reading
        WHERE measured_at_utc >= %s AND measured_at_utc <= %s
        ORDER BY measured_at_utc
    """, window)
    proposed = build_daily(day, p1, water, pv)
    existing = one(connection, "SELECT * FROM daily_usage WHERE usage_date=%s", (day,))
    _upsert(connection, "daily_usage", "usage_date", _merge_existing(proposed, existing))


def month_end(month):
    return date(month.year + (month.month == 12), month.month % 12 + 1, 1)


def build_monthly(month, daily_rows, through):
    if month.day != 1 or through <= month or through > month_end(month):
        raise ValueError("Ongeldig maandvenster")
    indexed = {row["usage_date"]: row for row in daily_rows}
    expected = [month + timedelta(days=offset)
                for offset in range((through - month).days)]
    result = {"usage_month": month}
    for metric in METRICS:
        result[metric] = None
    for commodity, metrics in COMMODITY_METRICS.items():
        qualities = [indexed[day][commodity + "_quality"] for day in expected
                     if day in indexed]
        complete = len(qualities) == len(expected) and all(
            quality in ("complete", "estimated") for quality in qualities)
        complete = complete and all(
            indexed[day][metric] is not None for day in expected for metric in metrics
            if day in indexed)
        if complete:
            for metric in metrics:
                result[metric] = sum((Decimal(str(indexed[day][metric]))
                                      for day in expected), Decimal("0"))
            result[commodity + "_quality"] = _quality(qualities)
            result[commodity + "_source"] = "daily_sum"
        else:
            result[commodity + "_quality"] = "unknown" if commodity != "pv" else "not_collected"
            result[commodity + "_source"] = "not_available"
    return result


def recalculate_month(connection, month, through):
    daily = rows(connection, """
        SELECT * FROM daily_usage WHERE usage_date >= %s AND usage_date < %s
        ORDER BY usage_date
    """, (month, through))
    proposed = build_monthly(month, daily, through)
    existing = one(connection, "SELECT * FROM monthly_usage WHERE usage_month=%s", (month,))
    _upsert(connection, "monthly_usage", "usage_month", _merge_existing(proposed, existing))


def run_once(connection, now=None):
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo("Europe/Amsterdam")).date()
    lookback = int(os.getenv("CALC_LOOKBACK_DAYS", "14"))
    if lookback < 1 or lookback > 366:
        raise ValueError("CALC_LOOKBACK_DAYS moet tussen 1 en 366 liggen")
    for offset in range(lookback, 0, -1):
        recalculate_day(connection, today - timedelta(days=offset))
    current_month = today.replace(day=1)
    previous_month = (current_month - timedelta(days=1)).replace(day=1)
    recalculate_month(connection, previous_month, current_month)
    if today > current_month:
        recalculate_month(connection, current_month, today)


def run_forever():
    import mysql.connector

    config = {"host": os.environ["DB_HOST"],
              "port": int(os.getenv("DB_PORT", "3306")),
              "user": os.environ["DB_USER"],
              "password": os.environ["DB_PASSWORD"],
              "database": "nuts_v2", "connection_timeout": 5}
    while True:
        try:
            connection = mysql.connector.connect(**config)
            try:
                run_once(connection)
            finally:
                connection.close()
            time.sleep(3600)
        except Exception as exc:
            print("Dagberekening wacht: {}".format(exc), flush=True)
            time.sleep(60)


if __name__ == "__main__":
    run_forever()
