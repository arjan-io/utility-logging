"""Zuivere omzetting van oude dag- en maandtotalen naar nuts_v2.

Deze module heeft geen database-afhankelijkheid. De live importer leest de
oude tabellen en schrijft de geretourneerde buckets naar het nieuwe schema.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Tuple


METRICS = (
    "import_low_kwh",
    "import_high_kwh",
    "export_low_kwh",
    "export_high_kwh",
    "gas_m3",
    "water_m3",
    "pv_generated_kwh",
)
COMMODITIES = ("electricity", "gas", "water", "pv")


def as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise ValueError("Onbekend datumtype: {!r}".format(value))


def period_key(value: object, grain: str) -> date:
    day = as_date(value)
    if grain == "month":
        if day.day != 1:
            raise ValueError(
                "Maandrecord heeft geen datum op de eerste dag: {}. "
                "Controleer de tijdzone van de MySQL-sessie.".format(day)
            )
        return day
    if grain == "day":
        return day
    raise ValueError("Ongeldige periode: {}".format(grain))


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _blank(period: date, grain: str) -> Dict[str, object]:
    bucket = {"usage_month" if grain == "month" else "usage_date": period}
    bucket.update({metric: None for metric in METRICS})
    for commodity in COMMODITIES:
        bucket[commodity + "_quality"] = "not_collected"
        bucket[commodity + "_source"] = "not_available"
    return bucket


def _one_row_per_period(
    rows: Iterable[Mapping[str, object]], grain: str, source: str
) -> Dict[date, Mapping[str, object]]:
    indexed = {}  # type: Dict[date, Mapping[str, object]]
    for row in rows:
        key = period_key(row["datum"], grain)
        if key in indexed:
            raise ValueError(
                "Dubbel {}-record voor {} in {} (ids {} en {}). "
                "Niet automatisch kiezen.".format(
                    grain, key, source, indexed[key].get("id"), row.get("id")
                )
            )
        indexed[key] = row
    return indexed


def build_legacy_usage(
    electricity_rows: Iterable[Mapping[str, object]],
    water_rows: Iterable[Mapping[str, object]],
    grain: str,
) -> Tuple[List[Dict[str, object]], List[str]]:
    """Merge oude stroom/gas en water per kalenderdag of -maand.

    Een maand maakt nooit dagbuckets aan. Negatieve volumes worden als
    onbekend gemarkeerd en in ``warnings`` gemeld, niet als verbruik bewaard.
    """

    electricity = _one_row_per_period(electricity_rows, grain, "elek/gas")
    water = _one_row_per_period(water_rows, grain, "water")
    warnings = []  # type: List[str]
    result = []  # type: List[Dict[str, object]]

    for period in sorted(set(electricity) | set(water)):
        bucket = _blank(period, grain)
        old_energy = electricity.get(period)
        if old_energy is not None:
            electric = {
                "import_low_kwh": _decimal(old_energy["glt"]),
                "import_high_kwh": _decimal(old_energy["ght"]),
                "export_low_kwh": _decimal(old_energy["llt"]),
                "export_high_kwh": _decimal(old_energy["lht"]),
            }
            bucket["electricity_source"] = "legacy_import"
            if all(value >= 0 for value in electric.values()):
                bucket.update(electric)
                bucket["electricity_quality"] = "estimated"
            else:
                bucket["electricity_quality"] = "unknown"
                warnings.append("{}: negatieve oude stroomwaarde".format(period))

            gas = _decimal(old_energy["gas"])
            bucket["gas_source"] = "legacy_import"
            if gas >= 0:
                bucket["gas_m3"] = gas
                bucket["gas_quality"] = "estimated"
            else:
                bucket["gas_quality"] = "unknown"
                warnings.append("{}: negatieve oude gaswaarde".format(period))

        old_water = water.get(period)
        if old_water is not None:
            liters = _decimal(old_water["liters"])
            bucket["water_source"] = "legacy_import"
            if liters >= 0:
                bucket["water_m3"] = liters / Decimal("1000")
                bucket["water_quality"] = "estimated"
                old_m3 = _decimal(old_water["m3"])
                if abs(old_m3 - bucket["water_m3"]) > Decimal("0.001"):
                    warnings.append(
                        "{}: water liters en m3 verschillen in {}".format(
                            period, grain
                        )
                    )
            else:
                bucket["water_quality"] = "unknown"
                warnings.append("{}: negatieve oude waterwaarde".format(period))

        result.append(bucket)

    return result, warnings
