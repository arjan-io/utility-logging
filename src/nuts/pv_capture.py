"""Poll one SEMS+ station and durably queue its cumulative generation.

The SEMS+ web response has no reliable measurement timestamp here, so the
UTC retrieval time is stored. No raw API response or credential is logged.
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from .outbox import Outbox, utc_timestamp
from .mqtt_topics import configured_topics


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

    return str(total), parse_power_w(data)


def parse_power_w(data):
    flow = (data.get("powerflow") or {}) if isinstance(data, dict) else {}
    if isinstance(flow, dict) and flow.get("pv") is not None:
        try:
            power, power_unit = _quantity(flow["pv"], {"W", "kW"}, "W")
            return int((power * (1000 if power_unit == "kW" else 1))
                       .quantize(Decimal(1), rounding=ROUND_HALF_UP))
        except ValueError:
            pass
    return None


def parse_day_kwh(data):
    """SEMS+ station and inverter daily counters are expressed in kWh."""
    if not isinstance(data, dict):
        return None
    kpi = data.get("kpi") or {}
    inverter = data.get("inverter") or {}
    if isinstance(inverter, list):
        inverter = inverter[0] if len(inverter) == 1 else {}
    full = inverter.get("invert_full") if isinstance(inverter, dict) else None
    values = []
    if isinstance(kpi, dict):
        values.append(kpi.get("power"))
    if isinstance(full, dict):
        values.append(full.get("eday"))
    for value in values:
        if value is None:
            continue
        try:
            amount, unit = _quantity(value, {"Wh", "kWh", "MWh"}, "kWh")
            factor = {"Wh": Decimal("0.001"), "kWh": Decimal(1),
                      "MWh": Decimal(1000)}[unit]
            return float(amount * factor)
        except ValueError:
            continue
    return None


def pv_payload(current_w, day_kwh, timestamp, day_fresh):
    """Keep the old GoodWe JSON paths while marking cached day values."""
    return {"pv": [{"pac": current_w, "eDag": day_kwh}],
            "measuredAtUtc": timestamp.replace(" ", "T") + "Z",
            "dayGenerationFresh": day_fresh}


def mqtt_client():
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.max_queued_messages_set(2)
    username = os.getenv("MQTT_USER")
    if username:
        client.username_pw_set(username, os.getenv("MQTT_PASSWORD"))
    client.connect_async(os.environ["MQTT_HOST"],
                         int(os.getenv("MQTT_PORT", "1883")))
    client.loop_start()
    return client


def process_reading(data, unit, timestamp, outbox, client, topic,
                    cached_day=None, compat_topic=None):
    """Store cumulative energy if available; publish live power independently."""
    current_w = parse_power_w(data)
    day_kwh = parse_day_kwh(data)
    day_fresh = day_kwh is not None
    if day_fresh:
        cached_day = day_kwh
    else:
        day_kwh = cached_day
    try:
        total, _ = parse_station_reading(data, unit)
        outbox.add_pv(total, current_w, timestamp)
    except ValueError as exc:
        print("PV-waarde onbruikbaar: {}".format(exc), flush=True)
    try:
        if current_w is not None and client.is_connected():
            payload = pv_payload(current_w, day_kwh, timestamp, day_fresh)
            encoded = json.dumps(payload)
            topics = (topic, compat_topic) if compat_topic and compat_topic != topic else (topic,)
            for target in topics:
                result = client.publish(target, encoded, qos=1)
                if result.rc != 0:
                    print("PV-MQTT wacht op broker: {}".format(result.rc), flush=True)
    except Exception as exc:
        print("PV-MQTT wacht: {}".format(type(exc).__name__), flush=True)
    return cached_day


def run_forever():
    from pygoodwe import SingleInverter

    interval = int(os.getenv("GOODWE_POLL_SECONDS", "60"))
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
    topics = configured_topics("pv")
    topic = topics[0]
    compat_topic = topics[1] if len(topics) > 1 else None
    print("PV-MQTT onderwerpen: {}".format(", ".join(topics)), flush=True)
    client = mqtt_client()
    local_tz = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "Europe/Amsterdam"))
    cached_day = None
    cached_date = None
    with Outbox(os.getenv("PV_OUTBOX_PATH", "/var/lib/nuts/pv.sqlite"), "pv") as outbox:
        while True:
            started = time.monotonic()
            try:
                data = station.get_current_readings(maxretries=1)
                timestamp = utc_timestamp()
                today = datetime.now(timezone.utc).astimezone(local_tz).date()
                if today != cached_date:
                    cached_day = None
                    cached_date = today
                cached_day = process_reading(data, unit, timestamp, outbox,
                                             client, topic, cached_day, compat_topic)
            except ValueError as exc:
                print("PV-waarde onbruikbaar: {}".format(exc), flush=True)
            except Exception as exc:
                print("PV-uitlezing wacht op SEMS+: {}".format(type(exc).__name__), flush=True)
            time.sleep(max(0, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    run_forever()
