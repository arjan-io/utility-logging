"""Bounded, read-only queries for the utility dashboard.

Raw timestamps are UTC DATETIME values. Calendar labels are Dutch local dates.
Unknown values remain null; an absent period is never presented as zero.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo


LOCAL = ZoneInfo("Europe/Amsterdam")
UTC = timezone.utc
METRICS = (
    "import_low_kwh", "import_high_kwh", "export_low_kwh", "export_high_kwh",
    "gas_m3", "water_m3", "pv_generated_kwh",
)
COMMODITIES = ("electricity", "gas", "water", "pv")


def number(value):
    return float(value) if value is not None else None


def utc_iso(value):
    return value.replace(tzinfo=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z") if value else None


def previous_month(month):
    return (month.replace(day=1) - timedelta(days=1)).replace(day=1)


def next_month(month):
    return (month.replace(day=28) + timedelta(days=4)).replace(day=1)


def query(connection, statement, params=()):
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(statement, params)
        return cursor.fetchall()
    finally:
        cursor.close()


def period(row, key):
    result = {"date": row[key].isoformat()}
    result.update({metric: number(row[metric]) for metric in METRICS})
    result["import_total_kwh"] = (result["import_low_kwh"] + result["import_high_kwh"]
                                  if result["import_low_kwh"] is not None and result["import_high_kwh"] is not None else None)
    result["export_total_kwh"] = (result["export_low_kwh"] + result["export_high_kwh"]
                                  if result["export_low_kwh"] is not None and result["export_high_kwh"] is not None else None)
    for commodity in COMMODITIES:
        result[commodity + "_quality"] = row[commodity + "_quality"]
    return result


def empty_period(day):
    return {"date": day.isoformat(), **{metric: None for metric in METRICS},
            "import_total_kwh": None, "export_total_kwh": None,
            **{commodity + "_quality": "unknown" for commodity in COMMODITIES}}


def slots(rows, first, last, key, step):
    by_date = {row[key]: period(row, key) for row in rows}
    result = []
    day = first
    while day <= last:
        result.append(by_date.get(day, empty_period(day)))
        day = step(day)
    return result


def quarter_hour(moment):
    return moment.replace(minute=(moment.minute // 15) * 15, second=0, microsecond=0)


def recent_power(rows, field, start, now):
    buckets = {}
    for row in rows:
        buckets[quarter_hour(row["measured_at_utc"])] = row
    result = []
    moment = quarter_hour(start)
    while moment <= now:
        row = buckets.get(moment)
        result.append({"time_utc": utc_iso(moment), "value": number(row[field]) if row else None})
        moment += timedelta(minutes=15)
    return result


def recent_water(rows, start, now):
    """An observed zero needs an event; no event means unknown, not zero."""
    buckets = defaultdict(lambda: {"liters": Decimal("0"), "observed": False})
    for row in rows:
        hour = row["measured_at_utc"].replace(minute=0, second=0, microsecond=0)
        buckets[hour]["observed"] = True
        buckets[hour]["liters"] += row["liters"]
    result = []
    hour = start.replace(minute=0, second=0, microsecond=0)
    while hour <= now:
        bucket = buckets.get(hour)
        result.append({"time_utc": utc_iso(hour),
                       "value": float(bucket["liters"]) if bucket and bucket["observed"] else None})
        hour += timedelta(hours=1)
    return result


def annual(months, current_year):
    result = []
    by_month = {row["date"]: row for row in months}
    for year in range(current_year - 4, current_year + 1):
        item = {"year": year, "months": []}
        for month_number in range(1, 13):
            key = date(year, month_number, 1).isoformat()
            item["months"].append(by_month.get(key, empty_period(date(year, month_number, 1))))
        result.append(item)
    return result


def daily_costs(rows, days, daily_rows):
    result = {}
    grouped = defaultdict(lambda: defaultdict(list))
    for row in rows:
        grouped[row["cost_date"].isoformat()][row["commodity"]].append(row)
    for day in days:
        item = {}
        usage = next((row for row in daily_rows if row["usage_date"] == day), None)
        for commodity in ("electricity", "gas", "water"):
            lines = grouped[day.isoformat()].get(commodity, [])
            if not lines or usage is None or usage[commodity + "_quality"] in ("unknown", "not_collected"):
                item[commodity] = None
                continue
            components = defaultdict(Decimal)
            for line in lines:
                components[line["component"]] += line["amount_eur"]
            measured = (sum((usage["import_low_kwh"] or 0, usage["import_high_kwh"] or 0))
                        if commodity == "electricity" else usage["gas_m3"] if commodity == "gas" else usage["water_m3"])
            if measured is None or (measured > 0 and "supply" not in components):
                item[commodity] = None
                continue
            item[commodity] = {
                "components": {key: float(value) for key, value in components.items()},
                "total_eur": float(sum(components.values(), Decimal("0"))),
                "excluding_network_eur": float(sum(
                    (amount for key, amount in components.items() if key != "network"), Decimal("0"))),
                "status": "final" if all(line["status"] == "final" for line in lines) else "estimate",
            }
        result[day.isoformat()] = item
    return result


def snapshot(connection, now=None):
    now = now or datetime.now(UTC)
    local_today = now.astimezone(LOCAL).date()
    yesterday = local_today - timedelta(days=1)
    first_day = yesterday - timedelta(days=30)
    current_month = local_today.replace(day=1)
    first_month = date(local_today.year - 4, 1, 1)
    last_month = previous_month(current_month)
    raw_start = (now - timedelta(hours=24)).replace(tzinfo=None)
    raw_end = now.replace(tzinfo=None)

    daily_rows = query(connection, """
        SELECT * FROM daily_usage WHERE usage_date BETWEEN %s AND %s
        ORDER BY usage_date
    """, (first_day, yesterday))
    month_rows = query(connection, """
        SELECT * FROM monthly_usage WHERE usage_month BETWEEN %s AND %s
        ORDER BY usage_month
    """, (first_month, last_month))
    cost_rows = query(connection, """
        SELECT cost_date, commodity, component, amount_eur, status
        FROM daily_cost_line WHERE cost_date BETWEEN %s AND %s
    """, (yesterday, local_today))
    p1 = query(connection, """
        SELECT measured_at_utc, import_w, export_w, gas_m3,
               import_low_kwh, import_high_kwh, export_low_kwh, export_high_kwh
        FROM p1_reading WHERE measured_at_utc BETWEEN %s AND %s
        ORDER BY measured_at_utc
    """, (raw_start, raw_end))
    pv = query(connection, """
        SELECT measured_at_utc, current_w, generated_total_kwh
        FROM pv_reading WHERE measured_at_utc BETWEEN %s AND %s
        ORDER BY measured_at_utc
    """, (raw_start, raw_end))
    water = query(connection, """
        SELECT measured_at_utc, liters FROM water_event
        WHERE measured_at_utc BETWEEN %s AND %s ORDER BY measured_at_utc
    """, (raw_start, raw_end))
    latest_p1 = p1[-1] if p1 else None
    latest_pv = pv[-1] if pv else None
    latest_water = water[-1] if water else None
    latest_gas = next((row for row in reversed(p1) if row["gas_m3"] is not None), None)
    p1_fresh = bool(latest_p1 and raw_end - latest_p1["measured_at_utc"] <= timedelta(minutes=2))
    pv_fresh = bool(latest_pv and latest_pv["current_w"] is not None
                    and raw_end - latest_pv["measured_at_utc"] <= timedelta(minutes=10))
    current = {
        "p1_at_utc": utc_iso(latest_p1["measured_at_utc"]) if latest_p1 else None,
        "pv_at_utc": utc_iso(latest_pv["measured_at_utc"]) if latest_pv else None,
        "water_at_utc": utc_iso(latest_water["measured_at_utc"]) if latest_water else None,
        "gas_at_utc": utc_iso(latest_gas["measured_at_utc"]) if latest_gas else None,
        "import_w": number(latest_p1["import_w"]) if p1_fresh else None,
        "export_w": number(latest_p1["export_w"]) if p1_fresh else None,
        "pv_w": number(latest_pv["current_w"]) if pv_fresh else None,
        "gas_meter_m3": number(latest_gas["gas_m3"]) if latest_gas and raw_end - latest_gas["measured_at_utc"] <= timedelta(hours=2) else None,
        "water_liters_per_min": (float(sum((row["liters"] for row in water
            if row["measured_at_utc"] >= raw_end - timedelta(minutes=3)), Decimal("0")) / Decimal("3"))
            if latest_water and raw_end - latest_water["measured_at_utc"] <= timedelta(minutes=10) else None),
    }
    current["home_w"] = (max(0, current["pv_w"] + current["import_w"] - current["export_w"])
                         if p1_fresh and pv_fresh else None)
    months = slots(month_rows, first_month, last_month, "usage_month", next_month)
    return {
        "generated_at_utc": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "timezone": "Europe/Amsterdam", "today": local_today.isoformat(),
        "current": current,
        "days": slots(daily_rows, first_day, yesterday, "usage_date", lambda day: day + timedelta(days=1)),
        "months": months,
        "years": annual(months, local_today.year),
        "costs": daily_costs(cost_rows, (yesterday, local_today), daily_rows),
        "recent": {
            "import_w": recent_power(p1, "import_w", raw_start, raw_end),
            "export_w": recent_power(p1, "export_w", raw_start, raw_end),
            "pv_w": recent_power(pv, "current_w", raw_start, raw_end),
            "water_liters_per_hour": recent_water(water, raw_start, raw_end),
        },
    }
