"""Recalculate estimated daily cost lines from local-day usage and tariffs.

Each component is stored separately. Settlement-dependent bands are not
guessed from one day of meter data; their final value belongs to settlement.
"""

import argparse
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from .tariff_logic import daily_equivalent, effective_amount


MONEY = Decimal("0.000001")
RATE = Decimal("0.00000001")
QUANTITY = Decimal("0.000001")
LOCAL = ZoneInfo("Europe/Amsterdam")


def rounded(value, unit):
    return value.quantize(unit, rounding=ROUND_HALF_UP)


def _meter_quantities(usage, commodity, flow, register):
    if flow not in ("import", "export"):
        return []
    quality = usage[commodity + "_quality"]
    if quality not in ("complete", "estimated"):
        return []
    if commodity == "electricity":
        prefix = "import" if flow == "import" else "export"
        registers = ("low", "high") if register == "all" else (register,)
        result = []
        for name in registers:
            value = usage[prefix + "_" + name + "_kwh"]
            if value is None:
                return []
            result.append((name, Decimal(str(value))))
        return result
    if flow == "export":
        return []
    value = usage["gas_m3" if commodity == "gas" else "water_m3"]
    return [("all", Decimal(str(value)))] if value is not None else []


def calculate_lines(day, usage, rules, electricity_policy=None):
    """Return cost rows, omitting unknown meter volumes and settlement bands."""
    lines = []
    for rule in rules:
        if rule["band_quantity"] is not None:
            continue
        commodity = rule["commodity"]
        component = rule["component"]
        basis = rule["basis"]
        gross = effective_amount(rule)
        if basis in ("per_day", "per_month", "per_year"):
            daily = daily_equivalent(gross, basis, day)
            quantities = [("fixed", daily / gross if gross else Decimal(1))]
        elif basis in ("per_kwh", "per_m3"):
            if (component == "export_compensation" and
                    electricity_policy != "separate_import_export"):
                continue
            quantities = _meter_quantities(
                usage, commodity, rule["flow"], rule["register_code"])
        else:
            continue
        for suffix, quantity in quantities:
            amount = (daily if suffix == "fixed" else quantity * gross)
            note = None
            if commodity == "electricity" and component in ("supply", "energy_tax") \
                    and electricity_policy == "saldering":
                note = "Afname vóór saldering; jaarafrekening kan dit wijzigen"
            lines.append({
                "cost_date": day, "tariff_rule_id": rule["id"],
                "commodity": commodity, "component": component,
                "line_key": "rule:{}:{}".format(rule["id"], suffix),
                "quantity": rounded(quantity, QUANTITY),
                "rate_basis": basis,
                "rate_amount_eur": rounded(gross, RATE),
                "amount_eur": rounded(amount, MONEY),
                "status": "estimate", "note": note,
            })
    return lines


def rows(connection, statement, params):
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(statement, params)
        return cursor.fetchall()
    finally:
        cursor.close()


def active_rules(connection, day):
    return rows(connection, """
        SELECT r.id, r.code, r.component, r.flow, r.register_code, r.basis,
               r.amount_eur, r.includes_vat, r.vat_rate, r.band_quantity,
               r.band_from, r.band_until,
               s.commodity, s.source_name
        FROM tariff_rule r JOIN tariff_source s ON s.id=r.tariff_source_id
        LEFT JOIN contract c ON c.id=s.contract_id
        WHERE r.status='active' AND r.valid_from<=%s
          AND (r.valid_until IS NULL OR r.valid_until>%s)
          AND (s.source_kind<>'supplier' OR
               (c.valid_from<=%s AND (c.valid_until IS NULL OR c.valid_until>%s)))
    """, (day, day, day, day))


def electricity_policy(connection, day):
    policies = rows(connection, """
        SELECT p.electricity_policy FROM settlement_period p
        JOIN contract c ON c.id=p.contract_id
        WHERE c.commodity='electricity' AND c.valid_from<=%s
          AND (c.valid_until IS NULL OR c.valid_until>%s)
          AND p.starts_on<=%s AND p.ends_on>%s
    """, (day, day, day, day))
    if len(policies) > 1:
        raise ValueError("Meerdere afrekenwijzen op {}".format(day))
    return policies[0]["electricity_policy"] if policies else None


def calculate_day(connection, day):
    usage_rows = rows(connection, "SELECT * FROM daily_usage WHERE usage_date=%s",
                      (day,))
    if not usage_rows:
        return None
    return calculate_lines(day, usage_rows[0], active_rules(connection, day),
                           electricity_policy(connection, day))


def save_day(connection, day):
    lines = calculate_day(connection, day)
    if lines is None:
        return 0
    final = rows(connection, """
        SELECT commodity, line_key FROM daily_cost_line
        WHERE cost_date=%s AND status='final'
    """, (day,))
    final_keys = {(row["commodity"], row["line_key"]) for row in final}
    lines = [line for line in lines
             if (line["commodity"], line["line_key"]) not in final_keys]
    cursor = connection.cursor()
    try:
        cursor.execute("DELETE FROM daily_cost_line WHERE cost_date=%s AND status='estimate'",
                       (day,))
        statement = """
            INSERT INTO daily_cost_line
            (cost_date, tariff_rule_id, commodity, component, line_key,
             quantity, rate_basis, rate_amount_eur, amount_eur,
             status, note, calculated_at_utc)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(3))
        """
        if lines:
            cursor.executemany(statement, [(
                line["cost_date"], line["tariff_rule_id"], line["commodity"],
                line["component"], line["line_key"], line["quantity"],
                line["rate_basis"], line["rate_amount_eur"], line["amount_eur"],
                line["status"], line["note"]
            ) for line in lines])
        connection.commit()
        return len(lines)
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def run_once(connection, now=None):
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(LOCAL).date()
    lookback = int(os.getenv("COST_LOOKBACK_DAYS", "35"))
    if lookback < 1 or lookback > 366:
        raise ValueError("COST_LOOKBACK_DAYS moet tussen 1 en 366 liggen")
    total = 0
    for offset in range(lookback, 0, -1):
        total += save_day(connection, today - timedelta(days=offset))
    return total


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
            print("Dagkosten wachten: {}".format(exc), flush=True)
            time.sleep(60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-day", type=date.fromisoformat,
                        help="toon kostenregels voor één lokale dag zonder te schrijven")
    parser.add_argument("--once", action="store_true",
                        help="herbereken de ingestelde terugkijkperiode eenmaal")
    args = parser.parse_args()
    if not args.check_day and not args.once:
        run_forever()
        return
    import mysql.connector
    connection = mysql.connector.connect(
        host=os.environ["DB_HOST"], port=int(os.getenv("DB_PORT", "3306")),
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
        database="nuts_v2", connection_timeout=5)
    try:
        if args.check_day:
            lines = calculate_day(connection, args.check_day)
            print(json.dumps({
                "day": args.check_day.isoformat(),
                "status": "no_daily_usage" if lines is None else "preview",
                "lines": [{key: str(value) if isinstance(value, Decimal) else
                           value.isoformat() if isinstance(value, date) else value
                           for key, value in line.items()} for line in (lines or [])],
            }, ensure_ascii=False))
        else:
            print("{} dagkostenregels herberekend".format(run_once(connection)))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
