"""Read-only SEMS+ probe for one GoodWe station.

Never print credentials, tokens, station IDs, serial numbers or raw responses.
The probe does not connect to MariaDB, SQLite or MQTT.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


_NUMERIC = re.compile(r"\s*([+-]?\d+(?:\.\d+)?)\s*(?:\(?(W|kW|Wh|kWh)\)?)?\s*")


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    match = _NUMERIC.fullmatch(str(value))
    if not match:
        return None
    try:
        number = Decimal(match.group(1))
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return str(number) + (" " + match.group(2) if match.group(2) else "")


def _first(mapping, *names):
    if not isinstance(mapping, dict):
        return None
    for name in names:
        value = _number(mapping.get(name))
        if value is not None:
            return value
    return None


def summarize(data):
    """Show only numeric monitoring fields; all identifiers stay private."""
    if not isinstance(data, dict):
        raise ValueError("SEMS+ gaf geen meetobject terug")
    kpi = data.get("kpi") or {}
    powerflow = data.get("powerflow") or {}
    inverter = data.get("inverter") or {}
    if isinstance(inverter, list):
        inverter = inverter[0] if inverter else {}
    details = inverter.get("d") or {} if isinstance(inverter, dict) else {}
    return {
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pv_flow": _first(powerflow, "pv"),
        "station_day_generation": _first(kpi, "power", "day_power"),
        "station_total_generation": _first(kpi, "total_power", "totalPower"),
        "inverter_day_generation": _first(details, "eDay", "eday", "e_day"),
        "inverter_total_generation": _first(details, "eTotal", "etotal", "e_total"),
    }


def main():
    station_id = os.getenv("GOODWE_STATION_ID")
    account = os.getenv("GOODWE_ACCOUNT")
    password = os.getenv("GOODWE_PASSWORD")
    if not all((station_id, account, password)):
        print("Vul GOODWE_STATION_ID, GOODWE_ACCOUNT en GOODWE_PASSWORD in .env in.", file=sys.stderr)
        return 2

    try:
        from pygoodwe import SingleInverter

        inverter = SingleInverter(
            system_id=station_id,
            account=account,
            password=password,
            skipload=True,
        )
        readings = inverter.get_current_readings(maxretries=1)
        result = summarize(readings)
        if all(value is None for key, value in result.items() if key != "retrieved_at_utc"):
            raise ValueError("Geen bruikbare PV-velden in de SEMS+-uitlezing")
    except Exception as exc:
        print("GoodWe-proef mislukt ({}). Geen ruwe API-data of tokens delen.".format(
            type(exc).__name__), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
