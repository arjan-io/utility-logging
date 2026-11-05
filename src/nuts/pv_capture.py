"""Poll one SEMS+ station and durably queue its cumulative generation.

The SEMS+ web response has no reliable measurement timestamp here, so the
UTC retrieval time is stored. No raw API response or credential is logged.
"""

import os
import re
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .outbox import Outbox, utc_timestamp


VALUE = re.compile(r"\s*([+-]?\d+(?:\.\d+)?)\s*(Wh|kWh|MWh|W|kW)?\s*", re.I)


def _quantity(value, allowed, fallback_unit):
    if value is None or isinstance(value, bool):
        raise ValueError("Meetwaarde ontbreekt")
    match = VALUE.fullmatch(str(value))
    if match is None:
        raise ValueError("Meetwaarde heeft onbekend formaat")
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise ValueError("Meetwaarde is ongeldig") from exc
    unit = match.group(2) or fallback_unit
    if not amount.is_finite() or amount < 0 or unit not in allowed:
        raise ValueError("Meetwaarde of eenheid is ongeldig")
    return amount, unit


def parse_station_reading(data, implicit_total_unit):
    """Return normalized kWh and optional W; require unit for bare totals."""
    if implicit_total_unit not in ("kWh", "MWh"):
        raise ValueError("GOODWE_TOTAL_UNIT moet kWh of MWh zijn")
    if not isinstance(data, dict):
        raise ValueError("SEMS+ gaf geen meetobject terug")
    kpi = data.get("kpi") or {}
    if not isinstance(kpi, dict):
        raise ValueError("SEMS+ KPI-velden ontbreken")
    inverter = data.get("inverter") or {}
    if isinstance(inverter, list):
        inverter = inverter[0] if len(inverter) == 1 else {}
    full = inverter.get("invert_full") if isinstance(inverter, dict) else None
    total_value = kpi.get("total_power")
    if total_value is None and isinstance(full, dict):
        total_value = full.get("etotal")
    amount, unit = _quantity(total_value,
                             {"Wh", "kWh", "MWh"}, implicit_total_unit)
    factor = {"Wh": Decimal("0.001"), "kWh": Decimal(1),
              "MWh": Decimal(1000)}[unit]
    total = (amount * factor).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

    flow = (data.get("powerflow") or {})
    current_w = None
    if isinstance(flow, dict) and flow.get("pv") is not None:
        try:
            power, power_unit = _quantity(flow["pv"], {"W", "kW"}, "W")
            current_w = int((power * (1000 if power_unit == "kW" else 1))
                            .quantize(Decimal(1), rounding=ROUND_HALF_UP))
        except ValueError:
            pass
    return str(total), current_w


def run_forever():
    from pygoodwe import SingleInverter

    interval = int(os.getenv("GOODWE_POLL_SECONDS", "300"))
    if interval < 60:
        raise ValueError("GOODWE_POLL_SECONDS moet minstens 60 zijn")
    unit = os.getenv("GOODWE_TOTAL_UNIT", "")
    if unit not in ("kWh", "MWh"):
        raise ValueError("GOODWE_TOTAL_UNIT moet kWh of MWh zijn")
    station = SingleInverter(
        system_id=os.environ["GOODWE_STATION_ID"],
        account=os.environ["GOODWE_ACCOUNT"],
        password=os.environ["GOODWE_PASSWORD"],
        skipload=True,
    )
    with Outbox(os.getenv("PV_OUTBOX_PATH", "/var/lib/nuts/pv.sqlite"), "pv") as outbox:
        while True:
            try:
                data = station.get_current_readings(maxretries=1)
                timestamp = utc_timestamp()
                total, current_w = parse_station_reading(data, unit)
                outbox.add_pv(total, current_w, timestamp)
            except ValueError as exc:
                print("PV-waarde onbruikbaar: {}".format(exc), flush=True)
            except Exception as exc:
                print("PV-uitlezing wacht op SEMS+: {}".format(type(exc).__name__), flush=True)
            time.sleep(interval)


if __name__ == "__main__":
    run_forever()
