"""Parse DSMR P1 telegrams without rounding cumulative meter readings."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Dict, Optional

from .outbox import utc_timestamp


REGISTER = {
    "1-0:1.8.1": "import_low_kwh",
    "1-0:1.8.2": "import_high_kwh",
    "1-0:2.8.1": "export_low_kwh",
    "1-0:2.8.2": "export_high_kwh",
}
POWER = {
    "1-0:1.7.0": "import_w",
    "1-0:2.7.0": "export_w",
    "1-0:21.7.0": "phase_l1_import_w",
    "1-0:41.7.0": "phase_l2_import_w",
    "1-0:61.7.0": "phase_l3_import_w",
    "1-0:62.7.0": "phase_l3_export_w",
}
VOLTAGE = {
    "1-0:32.7.0": "voltage_l1_v",
    "1-0:52.7.0": "voltage_l2_v",
    "1-0:72.7.0": "voltage_l3_v",
}
CURRENT = {
    "1-0:31.7.0": "current_l1_a",
    "1-0:51.7.0": "current_l2_a",
    "1-0:71.7.0": "current_l3_a",
}
VALUE = re.compile(r"^([^()]+)\(([0-9]+(?:\.[0-9]+)?)\*([^)]*)\)")
GAS = re.compile(r"^0-\d+:24\.2\.1\(([0-9]{12}[SW])\)\(([0-9]+(?:\.[0-9]+)?)\*m3\)")


def crc16(data: bytes) -> int:
    """DSMR CRC-16/Modbus over the slash-to-exclamation frame."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def _frame(telegram: bytes) -> bytes:
    start = telegram.find(b"/")
    if start < 0:
        raise ValueError("P1-start ontbreekt")
    end = telegram.find(b"!", start)
    if end < 0:
        raise ValueError("P1-einde ontbreekt")
    tail_lines = telegram[end + 1:].splitlines()
    tail = tail_lines[0].strip() if tail_lines else b""
    if not re.fullmatch(rb"[0-9A-Fa-f]{4}", tail):
        raise ValueError("P1-CRC ontbreekt of is ongeldig")
    actual = crc16(telegram[start:end + 1])
    expected = int(tail, 16)
    if actual != expected:
        raise ValueError("P1-CRC klopt niet: {:04X} != {:04X}".format(actual, expected))
    return telegram[start:end + 1]


def _gas_time(value: str) -> str:
    local = datetime.strptime(value[:12], "%y%m%d%H%M%S")
    offset = timedelta(hours=2 if value[-1] == "S" else 1)
    return utc_timestamp(local.replace(tzinfo=timezone(offset)))


def _decimal(value: str, unit: str, expected_unit: str) -> Decimal:
    if unit != expected_unit:
        raise ValueError("P1-eenheid {} verwacht, {} gekregen".format(expected_unit, unit))
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Ongeldig P1-getal: {}".format(value)) from exc
    if number < 0:
        raise ValueError("Negatieve meterstand")
    return number


def parse_telegram(telegram: bytes, received_at: Optional[datetime] = None) -> Dict[str, object]:
    """Return the values that fit the SQLite P1 outbox schema.

    The telegram's meter timestamp, if present, is used for its measurement.
    The gas register has a separate timestamp and is not silently dated as
    the electricity reading. Decimal values are stored as text in SQLite.
    """
    frame = _frame(telegram)
    try:
        lines = frame.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("Niet-ASCII P1-telegram") from exc
    readings: Dict[str, object] = {}
    for line in lines:
        line = line.strip()
        if line.startswith("0-0:1.0.0("):
            match = re.fullmatch(r"0-0:1\.0\.0\(([0-9]{12}[SW])\)", line)
            if not match:
                raise ValueError("Ongeldig P1-tijdstip")
            readings["measured_at_utc"] = _gas_time(match.group(1))
            continue
        if line.startswith("0-0:96.14.0("):
            match = re.fullmatch(r"0-0:96\.14\.0\(([0-9]+)\)", line)
            if match:
                readings["meter_tariff_code"] = int(match.group(1))
            continue
        gas = GAS.match(line)
        if gas:
            readings["gas_m3"] = str(_decimal(gas.group(2), "m3", "m3"))
            readings["gas_measured_at_utc"] = _gas_time(gas.group(1))
            continue
        value = VALUE.match(line)
        if not value:
            continue
        obis, raw, unit = value.groups()
        if obis in REGISTER:
            readings[REGISTER[obis]] = str(_decimal(raw, unit, "kWh"))
        elif obis in POWER:
            watts = _decimal(raw, unit, "kW") * 1000
            if watts != watts.to_integral_value():
                raise ValueError("P1-vermogen bevat fractie van watt")
            readings[POWER[obis]] = int(watts)
        elif obis in VOLTAGE:
            readings[VOLTAGE[obis]] = str(_decimal(raw, unit, "V"))
        elif obis in CURRENT:
            readings[CURRENT[obis]] = str(_decimal(raw, unit, "A"))
    for column in list(REGISTER.values()) + ["import_w", "export_w"]:
        if column not in readings:
            raise ValueError("P1-veld ontbreekt: {}".format(column))
    readings.setdefault("measured_at_utc", utc_timestamp(received_at))
    readings.setdefault("gas_m3", None)
    readings.setdefault("gas_measured_at_utc", None)
    return readings
