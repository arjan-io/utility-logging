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
from zoneinfo import ZoneInfo


_NUMERIC = re.compile(r"\s*([+-]?\d+(?:\.\d+)?)\s*(?:\(?(W|kW|Wh|kWh|MWh)\)?)?\s*")


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
    details = (inverter.get("invert_full") or inverter.get("d") or {}) if isinstance(inverter, dict) else {}
    station_day = _first(kpi, "power", "day_power")
    station_total = _first(kpi, "total_power", "totalPower")
    inverter_day = _first(details, "eday", "eDay", "e_day")
    inverter_total = _first(details, "etotal", "eTotal", "e_total")
    return {
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pv_flow": _first(powerflow, "pv"),
        "station_day_generation": station_day,
        "station_total_generation": station_total,
        "inverter_day_generation": inverter_day,
        "inverter_total_generation": inverter_total,
        "energy_counters_available": station_total is not None or inverter_total is not None,
    }


def probe_station_daily(inverter, station_id, now_local=None):
    """Try the separate SEMS+ station production endpoint, read-only."""
    now_local = now_local or datetime.now(ZoneInfo("Europe/Amsterdam"))
    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(hour=23, minute=59, second=59)
    payload = {
        "stationId": station_id,
        "items": ["proSystemTotalStats"],
        "dimension": "day",
        "isReport": False,
        "startTime": start.strftime("%Y-%m-%d %H:%M:%S"),
        "endTime": end.strftime("%Y-%m-%d %H:%M:%S"),
    }
    data = inverter._web_client().request(
        "/sems-plant/api/stations/production", method="POST", payload=payload,
    )
    return _first(data, "proSystemTotalStats", "production")


def main():
    station_id = os.getenv("GOODWE_STATION_ID")
    account = os.getenv("GOODWE_ACCOUNT")
    password = os.getenv("GOODWE_PASSWORD")
    if not all((station_id, account, password)):
        print("Vul GOODWE_STATION_ID, GOODWE_ACCOUNT en GOODWE_PASSWORD in .env in.", file=sys.stderr)
        return 2

    try:
        from pygoodwe import SingleInverter
        from pygoodwe.sems_plus import SemsPlusError

        inverter = SingleInverter(
            system_id=station_id,
            account=account,
            password=password,
            skipload=True,
        )
        readings = inverter.get_current_readings(maxretries=1)
        result = summarize(readings)
        result["station_day_fallback"] = None
        try:
            result["station_day_fallback"] = probe_station_daily(inverter, station_id)
            result["fallback_status"] = (
                "available" if result["station_day_fallback"] is not None else "unavailable"
            )
        except SemsPlusError as exc:
            result["fallback_status"] = exc.kind.value
        except Exception:
            result["fallback_status"] = "error"
        if all(result[key] is None for key in (
                "pv_flow", "station_day_generation", "station_total_generation",
                "inverter_day_generation", "inverter_total_generation",
                "station_day_fallback")):
            raise ValueError("Geen bruikbare PV-velden in de SEMS+-uitlezing")
    except Exception as exc:
        print("GoodWe-proef mislukt ({}). Geen ruwe API-data of tokens delen.".format(
            type(exc).__name__), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
