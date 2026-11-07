"""Read-only tariff and cost preview using fictitious meter usage."""

import argparse
import json
import os
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from . import costs
from .tariff_logic import current_rate_summary


LOCAL = ZoneInfo("Europe/Amsterdam")
MONEY = Decimal("0.01")
SAMPLE = {
    "electricity_quality": "complete", "gas_quality": "complete",
    "water_quality": "complete", "import_high_kwh": Decimal("4"),
    "import_low_kwh": Decimal("2"), "export_high_kwh": Decimal("1"),
    "export_low_kwh": Decimal("0"), "gas_m3": Decimal("1"),
    "water_m3": Decimal("0.1"),
}
COMMODITIES = ("electricity", "gas", "water")


def preview(connection, day):
    """Fetch effective rules and exercise the same calculation as daily costs."""
    rules = costs.active_rules(connection, day)
    policy = costs.electricity_policy(connection, day)
    lines = costs.calculate_lines(day, SAMPLE, rules, policy)
    summary, _ = current_rate_summary(rules, day)
    by_id = {rule["id"]: rule for rule in rules}
    totals = {commodity: Decimal(0) for commodity in COMMODITIES}
    without_network = totals.copy()
    details = []
    for line in lines:
        commodity = line["commodity"]
        amount = line["amount_eur"]
        totals[commodity] += amount
        if line["component"] != "network":
            without_network[commodity] += amount
        rule = by_id[line["tariff_rule_id"]]
        details.append({
            "commodity": commodity, "component": line["component"],
            "source": rule["source_name"], "code": rule["code"],
            "quantity": line["quantity"], "basis": line["rate_basis"],
            "rate_including_vat": line["rate_amount_eur"],
            "amount_eur": amount, "note": line["note"],
        })
    skipped = []
    for rule in rules:
        if rule["band_quantity"] is not None:
            reason = "afrekenband is niet uit één dag bekend"
        elif rule["component"] == "export_compensation" and policy != "separate_import_export":
            reason = "terugleververgoeding wordt pas bij afrekening bepaald"
        else:
            continue
        skipped.append({"commodity": rule["commodity"], "source": rule["source_name"],
                        "code": rule["code"], "reason": reason})
    warnings = []
    for commodity in COMMODITIES:
        current = [r for r in rules if r["commodity"] == commodity]
        if not any(r["component"] == "supply" for r in current):
            warnings.append("{}: geen variabele levering op deze datum".format(commodity))
        if commodity == "electricity":
            supply = [r for r in current if r["component"] == "supply" and
                      r["band_quantity"] is None]
            for register in ("low", "high"):
                if not any(r["register_code"] in (register, "all") for r in supply):
                    warnings.append("electricity: geen levering voor telwerk {}".format(register))
        if not any(r["component"] == "energy_tax" for r in current):
            warnings.append("{}: geen verbruiksbelasting op deze datum".format(commodity))
        if not any(r["component"] == "fixed_supply" for r in current):
            warnings.append("{}: geen vastrecht of vaste levering op deze datum".format(commodity))
    slots = {}
    for rule in rules:
        if rule["band_quantity"] is not None:
            continue
        slot = (rule["commodity"], rule["component"], rule["flow"],
                rule["register_code"], rule["basis"])
        slots.setdefault(slot, []).append(rule["code"])
    for slot, codes in slots.items():
        if len(codes) > 1:
            warnings.append("mogelijk dubbel tarief voor {}: {}".format(
                "/".join(slot), ", ".join(codes)))
    if any(r["commodity"] == "electricity" for r in rules) and policy is None:
        warnings.append("electricity: geen afrekenwijze voor deze datum")
    rates = {
        "electricity_low_eur_per_kwh": summary["electricity"]["low"],
        "electricity_high_eur_per_kwh": summary["electricity"]["high"],
        "gas_eur_per_m3": summary["gas"]["variable_m3"],
        "water_eur_per_m3": summary["water"]["variable_m3"],
    }
    return {
        "day": day.isoformat(), "read_only": True,
        "fictitious_usage": SAMPLE,
        "electricity_policy": policy,
        "active_tariff_count": len(rules),
        "variable_rates_including_vat": rates,
        "cost_lines": details,
        "sample_total_eur": {key: value.quantize(MONEY, rounding=ROUND_HALF_UP)
                             for key, value in totals.items()},
        "sample_without_network_eur": {
            key: value.quantize(MONEY, rounding=ROUND_HALF_UP)
            for key, value in without_network.items()},
        "not_in_daily_sample": skipped,
        "warnings": warnings,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", action="append", type=date.fromisoformat,
                        help="lokale kalenderdag; mag meerdere keren voorkomen")
    args = parser.parse_args()
    days = args.day or [datetime.now(LOCAL).date()]
    import mysql.connector
    connection = mysql.connector.connect(
        host=os.environ["DB_HOST"], port=int(os.getenv("DB_PORT", "3306")),
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
        database="nuts_v2", connection_timeout=5,
    )
    try:
        for day in days:
            print(json.dumps(preview(connection, day), ensure_ascii=False,
                             indent=2, default=str))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
