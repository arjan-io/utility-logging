"""Read-only estimate for one electricity contract year and its gas advance.

Measured months and days are combined with four prior matching calendar
months. Scheduled advances are not evidence of bank payments. All projected
amounts remain estimates, especially the settlement of solar export.
"""

import argparse
import calendar
import html
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from .costs import calculate_lines
from .tariff_logic import daily_equivalent, effective_amount, rows
from .tariff_overview import document, nav


METRICS = ("import_low_kwh", "import_high_kwh", "export_low_kwh",
           "export_high_kwh", "gas_m3")
BY_COMMODITY = {"electricity": METRICS[:4], "gas": ("gas_m3",)}
ZERO = Decimal("0")
CENT = Decimal("0.01")
LOCAL = ZoneInfo("Europe/Amsterdam")


def add_months(anchor, count):
    month_index = anchor.year * 12 + anchor.month - 1 + count
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


def contract_year(contract_start, today):
    """Anniversary windows have an exclusive end, including leap-day starts."""
    start = add_months(contract_start, 12 * (today.year - contract_start.year))
    if start > today:
        start = add_months(contract_start, 12 * (today.year - contract_start.year - 1))
    return start, add_months(start, 12)


def scheduled_payments(schedules, start, end):
    """Assign due dates to contract months, even when payment follows later.

    A schedule starts with the first contract month beginning on or after
    valid_from. Its first_due_on is the payment date for that month. This
    allows twelve monthly instalments for a year whose first bill is late.
    """
    payments = []
    for schedule in {row["id"]: row for row in schedules}.values():
        if schedule["status"] != "active":
            continue
        step = 1 if schedule["cadence"] == "monthly" else 3
        first_index = ((schedule["valid_from"].year - start.year) * 12
                       + schedule["valid_from"].month - start.month)
        if add_months(start, first_index) < schedule["valid_from"]:
            first_index += 1
        for index in range(12):
            service_day = add_months(start, index)
            if service_day >= end or service_day < schedule["valid_from"] or (
                    schedule["valid_until"] is not None
                    and service_day >= schedule["valid_until"]):
                continue
            elapsed = index - first_index
            if elapsed >= 0 and elapsed % step == 0:
                due = add_months(schedule["first_due_on"], elapsed)
                payments.append((due, Decimal(str(schedule["amount_eur"]))))
    return sorted(payments)


def historical_month_average(month, commodity, metrics, monthly, before):
    candidates = [row for key, row in monthly.items()
                  if key.month == month and key < before
                  and row[commodity + "_quality"] in ("complete", "estimated")
                  and all(row[metric] is not None for metric in metrics)]
    candidates.sort(key=lambda row: row["usage_month"], reverse=True)
    candidates = candidates[:4]
    if not candidates:
        return None
    return {metric: sum((Decimal(str(row[metric])) for row in candidates), ZERO)
            / Decimal(len(candidates)) for metric in metrics}


def day_usage(day, today, period_start, monthly, daily):
    """Return a day estimate and its measured/historical origin per utility."""
    month = day.replace(day=1)
    days_in_month = Decimal(calendar.monthrange(day.year, day.month)[1])
    current_month = today.replace(day=1)
    result = {metric: None for metric in METRICS}
    origins = {}
    for commodity, metrics in BY_COMMODITY.items():
        selected = None
        origin = "unknown"
        stored = monthly.get(month)
        if month < current_month and stored and (
                stored[commodity + "_quality"] in ("complete", "estimated") and
                all(stored[metric] is not None for metric in metrics)):
            selected = {metric: Decimal(str(stored[metric])) / days_in_month
                        for metric in metrics}
            origin = "month"
        elif month == current_month and day < today:
            stored_day = daily.get(day)
            if stored_day and stored_day[commodity + "_quality"] in ("complete", "estimated") \
                    and all(stored_day[metric] is not None for metric in metrics):
                selected = {metric: Decimal(str(stored_day[metric])) for metric in metrics}
                origin = "day"
        if selected is None:
            average = historical_month_average(day.month, commodity, metrics, monthly,
                                               period_start.replace(day=1))
            if average is not None:
                selected = {metric: value / days_in_month
                            for metric, value in average.items()}
                origin = "history"
        if selected is not None:
            result.update(selected)
        result[commodity + "_quality"] = "estimated" if selected is not None else "unknown"
        origins[commodity] = origin
    result["water_quality"] = "unknown"
    return result, origins


def rules_on(day, rules):
    return [rule for rule in rules if rule["valid_from"] <= day
            and (rule["valid_until"] is None or day < rule["valid_until"])
            and (rule["source_kind"] != "supplier" or
                 (rule["contract_from"] is not None and rule["contract_from"] <= day and
                  (rule["contract_until"] is None or day < rule["contract_until"])))
            and rule["commodity"] in BY_COMMODITY]


def policy_on(day, periods):
    active = [row["electricity_policy"] for row in periods
              if row["starts_on"] <= day < row["ends_on"]]
    return active[0] if len(active) == 1 else None


def summarize(start, end, today, monthly_rows, daily_rows, rules, policies, schedules):
    """Calculate the year in one pass without writing to MariaDB."""
    monthly = {row["usage_month"]: row for row in monthly_rows}
    daily = {row["usage_date"]: row for row in daily_rows}
    totals = defaultdict(lambda: ZERO)
    quantity_totals = defaultdict(lambda: ZERO)
    origins = defaultdict(int)
    warnings = set()
    salder_import = ZERO
    salder_export = ZERO
    salder_import_cost = ZERO
    incomplete = False
    incomplete_commodities = set()
    day = start
    while day < end:
        usage, sources = day_usage(day, today, start, monthly, daily)
        for commodity, source in sources.items():
            origins[(commodity, source)] += 1
            if source == "unknown":
                warnings.add("{}: ontbrekende verbruiksbasis".format(commodity))
                incomplete = True
                incomplete_commodities.add(commodity)
        active = rules_on(day, rules)
        for commodity in BY_COMMODITY:
            supply = [rule for rule in active if rule["commodity"] == commodity
                      and rule["component"] == "supply" and rule["flow"] == "import"]
            covered = (any(rule["register_code"] in ("low", "all") for rule in supply)
                       and any(rule["register_code"] in ("high", "all") for rule in supply)
                       if commodity == "electricity" else bool(supply))
            if not covered:
                warnings.add("{}: leveringstarief ontbreekt op een of meer dagen".format(commodity))
                incomplete = True
                incomplete_commodities.add(commodity)
            required = ("fixed_supply", "energy_tax", "network")
            if commodity == "electricity":
                required += ("tax_credit",)
            for component in required:
                if not any(rule["commodity"] == commodity and
                           rule["component"] == component for rule in active):
                    warnings.add("{}: {} ontbreekt op een of meer dagen".format(
                        commodity, component))
                    incomplete = True
                    incomplete_commodities.add(commodity)
        policy = policy_on(day, policies)
        if policy is None:
            warnings.add("Geen afrekenwijze voor stroom op een of meer dagen")
            incomplete = True
            incomplete_commodities.add("electricity")
        if (policy == "separate_import_export" and
                usage["electricity_quality"] != "unknown" and
                usage["export_low_kwh"] + usage["export_high_kwh"] > 0):
            for component in ("export_compensation", "feedin_cost"):
                if not any(rule["commodity"] == "electricity" and
                           rule["component"] == component for rule in active):
                    warnings.add("Stroom: {} ontbreekt op een of meer dagen".format(component))
                    incomplete = True
                    incomplete_commodities.add("electricity")
        lines = calculate_lines(day, usage, active, policy)
        for line in lines:
            totals[line["commodity"]] += line["amount_eur"]
            totals[(line["commodity"], line["component"])] += line["amount_eur"]
            if (policy == "saldering" and line["commodity"] == "electricity" and
                    line["component"] in ("supply", "energy_tax")):
                salder_import_cost += line["amount_eur"]
        for metric in METRICS:
            if usage[metric] is not None:
                quantity_totals[metric] += usage[metric]
        if policy == "saldering" and usage["electricity_quality"] != "unknown":
            salder_import += usage["import_low_kwh"] + usage["import_high_kwh"]
            salder_export += usage["export_low_kwh"] + usage["export_high_kwh"]
        day += timedelta(days=1)

    # The daily lines charge gross imports. The year-end offset is an
    # estimate at the blended import price; an invoice may allocate differently.
    salder_credit = (salder_import_cost * min(salder_export / salder_import, Decimal(1))
                     if salder_import > 0 else ZERO)
    totals["electricity"] -= salder_credit
    if salder_export > salder_import:
        warnings.add("Teruglevering boven saldeerbaar verbruik: vergoeding nog niet geraamd")
        incomplete = True
        incomplete_commodities.add("electricity")
    exported = quantity_totals["export_low_kwh"] + quantity_totals["export_high_kwh"]
    matched_band = False
    for rule in rules:
        if (rule["commodity"] != "electricity" or rule["component"] != "feedin_cost"
                or rule["band_quantity"] != "settlement_export_kwh"):
            continue
        lower = Decimal(str(rule["band_from"] or 0))
        upper = Decimal(str(rule["band_until"])) if rule["band_until"] is not None else None
        if exported < lower or (upper is not None and exported >= upper):
            continue
        day = start
        while day < end:
            if rule in rules_on(day, (rule,)):
                amount = daily_equivalent(effective_amount(rule), rule["basis"], day)
                if amount is None:
                    warnings.add("Terugleverband met onberekenbare eenheid")
                    incomplete = True
                    incomplete_commodities.add("electricity")
                else:
                    matched_band = True
                    totals["electricity"] += amount
                    totals[("electricity", "feedin_cost_band")] += amount
            day += timedelta(days=1)
    if any(rule["component"] == "feedin_cost" and rule["band_quantity"] for rule in rules) \
            and not matched_band:
        warnings.add("Geen passende terugleverband voor de geraamde jaaropbrengst")
        incomplete = True
        incomplete_commodities.add("electricity")

    payments = scheduled_payments(schedules, start, end)
    if not payments:
        warnings.add("Geen geplande voorschotten voor dit contractjaar")
    due = sum((amount for _, amount in payments), ZERO)
    elapsed = sum((amount for when, amount in payments if when < today), ZERO)
    remaining = [(when, amount) for when, amount in payments if when >= today]
    expected_cost = None if incomplete else totals["electricity"] + totals["gas"]
    adjusted = ((expected_cost - elapsed) / Decimal(len(remaining))) \
        if expected_cost is not None and remaining else None
    return {
        "start": start, "end": end, "today": today,
        "electricity_eur": (None if "electricity" in incomplete_commodities
                            else totals["electricity"]),
        "gas_eur": None if "gas" in incomplete_commodities else totals["gas"],
        "expected_cost_eur": expected_cost, "salder_credit_eur": salder_credit,
        "export_kwh": exported, "scheduled_eur": due, "scheduled_past_eur": elapsed,
        "scheduled_count": len(payments),
        "scheduled_after_end_count": sum(1 for when, _ in payments if when >= end),
        "scheduled_after_end_eur": sum((amount for when, amount in payments
                                         if when >= end), ZERO),
        "remaining_count": len(remaining),
        "remaining_current_eur": sum((amount for _, amount in remaining), ZERO),
        "required_per_remaining_eur": adjusted,
        "origins": dict(origins), "warnings": sorted(warnings),
        "complete": not incomplete,
    }


def report(connection, today=None):
    today = today or datetime.now(LOCAL).date()
    contract = rows(connection, """
        SELECT id, valid_from FROM contract WHERE commodity='electricity'
          AND valid_from<=%s AND (valid_until IS NULL OR valid_until>%s)
        ORDER BY valid_from DESC LIMIT 1
    """, (today, today))
    if not contract:
        return None
    start, end = contract_year(contract[0]["valid_from"], today)
    gas = rows(connection, """
        SELECT id FROM contract WHERE commodity='gas'
          AND valid_from<%s AND (valid_until IS NULL OR valid_until>%s)
    """, (end, start))
    ids = [contract[0]["id"]] + [row["id"] for row in gas]
    monthly = rows(connection, "SELECT * FROM monthly_usage WHERE usage_month>=%s "
                   "AND usage_month<%s ORDER BY usage_month",
                   (date(start.year - 5, 1, 1), today.replace(day=1)))
    daily = rows(connection, "SELECT * FROM daily_usage WHERE usage_date>=%s "
                 "AND usage_date<%s", (today.replace(day=1), today))
    rules = rows(connection, """
        SELECT r.id, r.component, r.flow, r.register_code, r.basis,
               r.amount_eur, r.includes_vat, r.vat_rate, r.band_quantity,
               r.band_from, r.band_until, r.valid_from, r.valid_until,
               s.commodity, s.source_name, s.source_kind,
               c.valid_from AS contract_from, c.valid_until AS contract_until
        FROM tariff_rule r JOIN tariff_source s ON s.id=r.tariff_source_id
        LEFT JOIN contract c ON c.id=s.contract_id
        WHERE r.status='active' AND r.valid_from<%s
          AND (r.valid_until IS NULL OR r.valid_until>%s)
    """, (end, start))
    policies = rows(connection, """
        SELECT p.starts_on, p.ends_on, p.electricity_policy
        FROM settlement_period p JOIN contract c ON c.id=p.contract_id
        WHERE c.commodity='electricity' AND p.starts_on<%s AND p.ends_on>%s
    """, (end, start))
    schedules = rows(connection, """
        SELECT DISTINCT s.id, s.amount_eur, s.cadence, s.valid_from,
               s.valid_until, s.first_due_on, s.status
        FROM advance_schedule s JOIN advance_schedule_contract l
          ON l.schedule_id=s.id
        WHERE l.contract_id IN ({}) AND s.valid_from<%s
          AND (s.valid_until IS NULL OR s.valid_until>%s)
    """.format(",".join(["%s"] * len(ids))), tuple(ids) + (end, start))
    return summarize(start, end, today, monthly, daily, rules, policies, schedules)


def euros(value):
    if value is None:
        return "onbekend"
    amount = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    return "€ {:,.2f}".format(amount).replace(",", "_").replace(".", ",").replace("_", ".")


def page(connection, today=None):
    result = report(connection, today)
    if result is None:
        return document("Verwachte nota", nav() +
                        "<h1>Verwachte nota</h1><p>Er is geen lopend stroomcontract gevonden.</p>")
    esc = lambda value: html.escape(str(value), quote=True)
    balance = (result["scheduled_eur"] - result["expected_cost_eur"]
               if result["expected_cost_eur"] is not None else None)
    remaining = result["remaining_count"]
    current = (result["remaining_current_eur"] / Decimal(remaining)
               if remaining else None)
    required = result["required_per_remaining_eur"]
    shown_required = max(required, ZERO) if required is not None else None
    adjustment = (shown_required - current if shown_required is not None
                  and current is not None else None)
    warning_items = "".join("<li>{}</li>".format(esc(item)) for item in result["warnings"])
    warning_box = ("<section><h2>Onzekerheden</h2><ul>{}</ul></section>".format(warning_items)
                   if warning_items else "")
    late_note = (
        "<p class=\"hint\">{} termijn(en), samen {}, hebben een betaaldatum "
        "na {}. Ze horen volgens het ingevoerde schema nog bij dit "
        "contractjaar.</p>".format(result["scheduled_after_end_count"],
                                   euros(result["scheduled_after_end_eur"]),
                                   esc(result["end"] - timedelta(days=1)))
        if result["scheduled_after_end_count"] else "")
    sources = result["origins"]
    def source_row(commodity, label):
        return ("<tr><th>{}</th><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>"
                .format(label, sources.get((commodity, "month"), 0),
                        sources.get((commodity, "day"), 0),
                        sources.get((commodity, "history"), 0),
                        sources.get((commodity, "unknown"), 0)))
    suggestion = ("<p>Voor een nieuw maandbedrag ontbreken nog gegevens.</p>"
                  if required is None and remaining else
                  "<p>Er zijn geen resterende geplande betalingen in deze periode.</p>"
                  if not remaining else
                  "<p>Om op een saldo van ongeveer nul uit te komen: "
                  "<strong>{} per resterende betaling</strong> (nu gemiddeld {}; "
                  "verschil {}).</p>".format(
                      euros(shown_required), euros(current), euros(adjustment)))
    body = """{nav}<h1>Verwachte nota</h1>
<p>Contractjaar: <strong>{start} t/m {last}</strong>. Dit is een raming voor stroom en gas,
inclusief vaste levering, belastingen en netbeheer. Water staat hier buiten.</p>
<section><h2>Jaarprognose</h2><table>
<tr><th>Stroom</th><td>{electricity}</td></tr>
<tr><th>Gas</th><td>{gas}</td></tr>
<tr><th>Verwachte jaarkosten</th><td><strong>{total}</strong></td></tr>
<tr><th>Waarvan geraamde saldering</th><td>− {salder}</td></tr>
<tr><th>Geplande voorschotten ({scheduled_count} termijnen)</th><td>{scheduled}</td></tr>
<tr><th>Verwacht saldo voorschot minus kosten</th><td><strong>{balance}</strong></td></tr>
</table>{late_note}<p class="hint">Een positief saldo wijst op een mogelijk tegoed;
een negatief saldo op mogelijk bijbetalen. Voorschotten zijn hier
<em>gepland</em>, niet als bankbetaling bevestigd.</p></section>
<section><h2>Resterend voorschot</h2>
<p>Volgens het schema zijn tot vandaag {past} aan voorschotten gepland.
Er volgen nog {count} betaling(en), samen {future}.</p>{suggestion}
<p class="hint">Bij een maandelijks voorschot is het voorgestelde bedrag
het bedrag voor elke resterende maand.</p>
<p class="hint">Een negatief berekend nieuw bedrag wordt als € 0,00 getoond;
een verwacht teveel betaald bedrag kan pas via de echte nota worden verrekend.</p></section>
<section><h2>Basis van de raming</h2>
<p>Afgesloten maanden komen uit de maandtotalen. De huidige maand gebruikt
beschikbare dagwaarden; overige dagen volgen het gemiddelde van maximaal vier
eerdere gelijknamige maanden. Bij een gedeeltelijke maand wordt het
maandtotaal gelijk over de kalenderdagen verdeeld.</p><table><tr><th></th><th>Maandwaarde</th>
<th>Dagwaarde</th><th>Historisch gemiddelde</th><th>Onbekend</th></tr>
{electricity_sources}{gas_sources}</table>
<p class="hint">De zonnepanelenverrekening is een schatting op basis van
geraamde afname en teruglevering. Tarieven kunnen tijdens dit contractjaar
wijzigen; een verbruiksafhankelijke terugleverband blijft afhankelijk van de
uiteindelijke jaaropbrengst.</p></section>{warnings}""".format(
        nav=nav(), start=esc(result["start"]),
        last=esc(result["end"] - timedelta(days=1)),
        electricity=euros(result["electricity_eur"]),
        gas=euros(result["gas_eur"]), total=euros(result["expected_cost_eur"]),
        salder=euros(result["salder_credit_eur"]),
        scheduled=euros(result["scheduled_eur"]),
        scheduled_count=result["scheduled_count"], late_note=late_note,
        balance=euros(balance),
        past=euros(result["scheduled_past_eur"]), count=remaining,
        future=euros(result["remaining_current_eur"]), suggestion=suggestion,
        electricity_sources=source_row("electricity", "Stroom"),
        gas_sources=source_row("gas", "Gas"), warnings=warning_box)
    return document("Verwachte nota", body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="toon de nota als JSON zonder te schrijven")
    args = parser.parse_args()
    if not args.check:
        parser.error("Gebruik --check voor een alleen-lezen controle")
    import mysql.connector
    connection = mysql.connector.connect(
        host=os.environ["DB_HOST"], port=int(os.getenv("DB_PORT", "3306")),
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
        database="nuts_v2", connection_timeout=5)
    try:
        result = report(connection)
        if result is not None:
            result = dict(result, origins={
                "{}:{}".format(commodity, source): count
                for (commodity, source), count in result["origins"].items()})
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
