"""Publish the existing MQTT payloads from nuts_v2 on a configurable prefix.

Costs here are variable import costs, as in the old MQTT scripts. A missing
boundary reading or tariff produces JSON null instead of a false zero.
"""

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from .usage import day_start_utc


LOCAL = ZoneInfo("Europe/Amsterdam")
ZERO = Decimal("0")
CENT = Decimal("0.01")


def sql_time(moment):
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def aware(moment):
    return moment.replace(tzinfo=timezone.utc)


def money(value):
    if value is None:
        return None
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))


class MeterRepository:
    def __init__(self, connection):
        self.connection = connection

    def one(self, query, params=()):
        cursor = self.connection.cursor(dictionary=True)
        try:
            cursor.execute(query, params)
            return cursor.fetchone()
        finally:
            cursor.close()

    def rows(self, query, params=()):
        cursor = self.connection.cursor(dictionary=True)
        try:
            cursor.execute(query, params)
            return cursor.fetchall()
        finally:
            cursor.close()

    def latest_p1(self):
        return self.one("""
            SELECT measured_at_utc, import_w, export_w,
                   phase_l1_import_w, phase_l2_import_w, phase_l3_import_w,
                   phase_l3_export_w, voltage_l1_v, voltage_l2_v,
                   voltage_l3_v, current_l1_a, current_l2_a, current_l3_a
            FROM p1_reading ORDER BY measured_at_utc DESC LIMIT 1
        """)

    def electric_at(self, moment):
        return self.one("""
            SELECT measured_at_utc, import_low_kwh, import_high_kwh,
                   export_low_kwh, export_high_kwh
            FROM p1_reading WHERE measured_at_utc <= %s
            ORDER BY measured_at_utc DESC LIMIT 1
        """, (sql_time(moment),))

    def gas_at(self, moment):
        return self.one("""
            SELECT gas_measured_at_utc, gas_m3
            FROM p1_reading
            WHERE gas_measured_at_utc <= %s AND gas_m3 IS NOT NULL
            ORDER BY gas_measured_at_utc DESC, measured_at_utc DESC LIMIT 1
        """, (sql_time(moment),))

    def water_events(self, start, end):
        margin = timedelta(minutes=75)
        return self.rows("""
            SELECT measured_at_utc, liters, source FROM water_event
            WHERE measured_at_utc >= %s AND measured_at_utc <= %s
            ORDER BY measured_at_utc
        """, (sql_time(start - margin), sql_time(end + margin)))

    def water_flow(self, now):
        row = self.one("""
            SELECT COALESCE(SUM(liters), 0) AS liters
            FROM water_event WHERE measured_at_utc >= %s
              AND measured_at_utc < %s
        """, (sql_time(now - timedelta(minutes=3)), sql_time(now)))
        return float(Decimal(str(row["liters"])) / Decimal("3"))

    def gas_rate(self, now):
        rows = self.rows("""
            SELECT DISTINCT gas_measured_at_utc, gas_m3
            FROM p1_reading
            WHERE gas_measured_at_utc >= %s
              AND gas_measured_at_utc <= %s AND gas_m3 IS NOT NULL
            ORDER BY gas_measured_at_utc DESC LIMIT 3
        """, (sql_time(now - timedelta(minutes=10)), sql_time(now)))
        if len(rows) < 2:
            return 0.0
        latest, previous = rows[0], rows[1]
        seconds = (latest["gas_measured_at_utc"] - previous["gas_measured_at_utc"]).total_seconds()
        delta = Decimal(str(latest["gas_m3"])) - Decimal(str(previous["gas_m3"]))
        return round(float(delta * Decimal("3600") / Decimal(str(seconds))), 2) \
            if seconds > 0 and delta > 0 else 0.0

    def variable_rates(self, commodity, day):
        rows = self.rows("""
            SELECT r.component, r.register_code, r.amount_eur,
                   r.includes_vat, r.vat_rate, r.band_quantity
            FROM tariff_rule AS r
            JOIN tariff_source AS s ON s.id = r.tariff_source_id
            LEFT JOIN contract AS c ON c.id = s.contract_id
            WHERE s.commodity = %s AND r.status = 'active'
              AND r.valid_from <= %s
              AND (r.valid_until IS NULL OR r.valid_until > %s)
              AND r.component IN ('supply', 'energy_tax')
              AND r.flow IN ('import', 'none')
              AND r.basis = %s
              AND (s.source_kind <> 'supplier' OR
                   (c.valid_from <= %s AND
                    (c.valid_until IS NULL OR c.valid_until > %s)))
        """, (commodity, day, day,
              "per_kwh" if commodity == "electricity" else "per_m3",
              day, day))
        if any(row["band_quantity"] is not None for row in rows):
            return None  # a settlement band cannot be chosen per day
        required = {"supply", "energy_tax"} if commodity != "water" else {"supply"}
        if not required.issubset({row["component"] for row in rows}):
            return None
        rates = {"low": ZERO, "high": ZERO}
        for row in rows:
            amount = Decimal(str(row["amount_eur"]))
            if not row["includes_vat"]:
                amount *= 1 + Decimal(str(row["vat_rate"]))
            register = row["register_code"]
            for key in ("low", "high"):
                if register in ("all", key):
                    rates[key] += amount
        if rates["low"] == 0 or (commodity == "electricity" and rates["high"] == 0):
            return None
        return rates


def meter_delta(start, end, keys, timestamp_key, max_age):
    if start is None or end is None:
        return None
    if (max_age[0] - aware(start[timestamp_key]) > max_age[2] or
            max_age[1] - aware(end[timestamp_key]) > max_age[2]):
        return None
    values = {}
    for key in keys:
        if start[key] is None or end[key] is None:
            return None
        difference = Decimal(str(end[key])) - Decimal(str(start[key]))
        if difference < 0:
            return None
        values[key] = difference
    return values


def water_volume(events, start, end, complete_day):
    """Return m3 when heartbeat/event coverage supports the period."""
    points = [(aware(row["measured_at_utc"]), Decimal(str(row["liters"])),
               row.get("source", "collector")) for row in events]
    if not points:
        return None
    before = [point for point in points if point[0] <= start]
    after = [point for point in points if point[0] >= end]

    def allowance(left, right):
        return timedelta(minutes=75 if left[2] == right[2] == "legacy_import" else 15)

    if not before or start - before[-1][0] > timedelta(
            minutes=75 if before[-1][2] == "legacy_import" else 15):
        return None
    if complete_day:
        if not after or after[0][0] - end > timedelta(
                minutes=75 if after[0][2] == "legacy_import" else 15):
            return None
    else:
        latest = [point for point in points if point[0] <= end]
        if not latest or end - latest[-1][0] > timedelta(
                minutes=75 if latest[-1][2] == "legacy_import" else 15):
            return None
    interior = [point for point in points if start <= point[0] <= end]
    anchors = [before[-1]] + interior + ([after[0]] if complete_day else [])
    if any(right[0] - left[0] > allowance(left, right)
           for left, right in zip(anchors, anchors[1:])):
        return None
    liters = sum((liters for moment, liters, _ in points if start <= moment < end), ZERO)
    return liters / Decimal("1000")


def electricity_payload(row):
    if row is None:
        return None
    number = lambda value: float(value) if value is not None else None
    return {
        "gebruik": {"hv": row["import_w"], "hl": row["export_w"]},
        "voltage": {"v1": number(row["voltage_l1_v"]),
                    "v2": number(row["voltage_l2_v"]),
                    "v3": number(row["voltage_l3_v"])},
        "energie": {"p1": row["phase_l1_import_w"],
                    "p2": row["phase_l2_import_w"],
                    "p3": row["phase_l3_import_w"],
                    "p3l": row["phase_l3_export_w"]},
        "stroom": {"a1": number(row["current_l1_a"]),
                   "a2": number(row["current_l2_a"]),
                   "a3": number(row["current_l3_a"])},
        "measuredAtUtc": row["measured_at_utc"].isoformat() + "Z",
    }


def costs_for_period(repo, commodity, day, start, end, complete_day):
    rates = repo.variable_rates(commodity, day)
    if rates is None:
        return None
    if commodity == "electricity":
        delta = meter_delta(repo.electric_at(start), repo.electric_at(end),
                            ("import_low_kwh", "import_high_kwh"),
                            "measured_at_utc", (start, end, timedelta(minutes=5)))
        if delta is None:
            return None
        return (delta["import_low_kwh"] * rates["low"] +
                delta["import_high_kwh"] * rates["high"])
    if commodity == "gas":
        delta = meter_delta(repo.gas_at(start), repo.gas_at(end),
                            ("gas_m3",), "gas_measured_at_utc",
                            (start, end, timedelta(minutes=15)))
        return delta["gas_m3"] * rates["low"] if delta else None
    events = repo.water_events(start, end)
    volume = water_volume(events, start, end, complete_day)
    return volume * rates["low"] if volume is not None else None


def build_payloads(repo, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now moet een tijdzone hebben")
    today = now.astimezone(LOCAL).date()
    yesterday = today - timedelta(days=1)
    periods = ((today, day_start_utc(today), now, False),
               (yesterday, day_start_utc(yesterday), day_start_utc(today), True))
    costs = {}
    for commodity in ("electricity", "gas", "water"):
        costs[commodity] = [money(costs_for_period(repo, commodity, *period))
                            for period in periods]
    latest = repo.latest_p1()
    return {
        "dsmr/electricity": electricity_payload(latest),
        "dsmr/kosten": {"kostenVandaag": costs["electricity"][0],
                         "kostenGister": costs["electricity"][1]},
        "dsmr/gas": {"rate": repo.gas_rate(now),
                     "costsToday": costs["gas"][0],
                     "costsYesterday": costs["gas"][1]},
        "water": {"rate": round(repo.water_flow(now), 2),
                  "costsToday": costs["water"][0],
                  "costsYesterday": costs["water"][1]},
    }


def run_forever():
    import mysql.connector
    import paho.mqtt.publish as publish

    config = {"host": os.environ["DB_HOST"],
              "port": int(os.getenv("DB_PORT", "3306")),
              "user": os.environ["DB_USER"],
              "password": os.environ["DB_PASSWORD"],
              "database": "nuts_v2", "connection_timeout": 5}
    prefix = os.getenv("MQTT_PREFIX", "sensors/v2").rstrip("/")
    mqtt_host = os.environ["MQTT_HOST"]
    mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
    mqtt_user = os.getenv("MQTT_USER")
    auth = {"username": mqtt_user, "password": os.getenv("MQTT_PASSWORD")}
    if not mqtt_user:
        auth = None
    while True:
        try:
            connection = mysql.connector.connect(**config)
            try:
                payloads = build_payloads(MeterRepository(connection))
            finally:
                connection.close()
            for suffix, payload in payloads.items():
                if payload is None:
                    continue
                publish.single(prefix + "/" + suffix,
                               payload=json.dumps(payload), hostname=mqtt_host,
                               port=mqtt_port, auth=auth, qos=1)
            time.sleep(30)
        except Exception as exc:
            print("Publisher wacht: {}".format(exc), flush=True)
            time.sleep(10)


if __name__ == "__main__":
    run_forever()
