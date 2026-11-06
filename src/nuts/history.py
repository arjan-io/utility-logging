"""Zuivere omzetting van oude dag- en maandtotalen naar nuts_v2.

Deze module heeft geen database-afhankelijkheid. De live importer leest de
oude tabellen en schrijft de geretourneerde buckets naar het nieuwe schema.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Tuple
from zoneinfo import ZoneInfo


LOCAL = ZoneInfo("Europe/Amsterdam")


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
        # The importer requests TIMESTAMP columns in UTC. DATETIME values in
        # the old period tables are also interpreted as UTC clock times.
        utc = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return utc.astimezone(LOCAL).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        value = value.strip()
        return as_date(datetime.fromisoformat(value)) if len(value) > 10 else date.fromisoformat(value)
    raise ValueError("Onbekend datumtype: {!r}".format(value))


def period_key(value: object, grain: str) -> date:
    day = as_date(value)
    if grain == "month":
        if day.day != 1:
            raise ValueError(
                "Maandrecord heeft geen datum op de eerste dag: {}. "
                "Controleer de brondatum en tijdzone.".format(day)
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


def _pv_by_period(rows, grain, warnings):
    by_day = {}  # type: Dict[date, List[Mapping[str, object]]]
    for row in rows:
        day = as_date(row["datum"])
        by_day.setdefault(day, []).append(row)
    indexed = {}
    ambiguous_months = set()
    month_max = {}
    for day, candidates in sorted(by_day.items()):
        if len(candidates) > 1 and grain == "day":
            warnings.append(
                "{}: {} oude PV-dagregels ({}); dagopbrengst onbekend, "
                "controleer brondatums".format(
                    day, len(candidates), "; ".join(
                        "id {}: eDag={}, eMaand={}, eTotal={}".format(
                            row.get("id"), row.get("eDag"), row.get("eMaand"),
                            row.get("eTotal", "?")) for row in candidates)))
        if grain == "day":
            indexed[day] = candidates[0] if len(candidates) == 1 else None
        else:
            # eMaand is an intermittently noisy cloud snapshot. Only the
            # latest dated snapshot estimates a month's total. A duplicate
            # on that final date makes the month itself ambiguous.
            month = day.replace(day=1)
            indexed[month] = candidates[0] if len(candidates) == 1 else None
            if len(candidates) > 1:
                ambiguous_months.add(month)
            else:
                ambiguous_months.discard(month)
                value = _decimal(candidates[0]["eMaand"])
                month_max[month] = max(value, month_max.get(month, value))
    for month in sorted(ambiguous_months):
        warnings.append(
            "{}: laatste PV-maandstand heeft dubbele bronregels; "
            "maandopbrengst onbekend".format(month))
    if grain == "month":
        for month, row in indexed.items():
            if row is not None and _decimal(row["eMaand"]) < month_max[month]:
                indexed[month] = None
                warnings.append(
                    "{}: laatste PV-maandstand {} is lager dan eerdere stand {}; "
                    "maandopbrengst onbekend".format(
                        month, row["eMaand"], month_max[month]))
    return indexed


def build_legacy_usage(
    electricity_rows: Iterable[Mapping[str, object]],
    water_rows: Iterable[Mapping[str, object]],
    grain: str,
    pv_rows: Iterable[Mapping[str, object]] = (),
    pv_factor: Decimal = Decimal("1"),
) -> Tuple[List[Dict[str, object]], List[str]]:
    """Merge oude stroom/gas en water per kalenderdag of -maand.

    Een maand maakt nooit dagbuckets aan. Negatieve volumes worden als
    onbekend gemarkeerd en in ``warnings`` gemeld, niet als verbruik bewaard.
    """

    electricity = _one_row_per_period(electricity_rows, grain, "elek/gas")
    water = _one_row_per_period(water_rows, grain, "water")
    warnings = []  # type: List[str]
    pv = _pv_by_period(pv_rows, grain, warnings)
    result = []  # type: List[Dict[str, object]]

    for period in sorted(set(electricity) | set(water) | set(pv)):
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

        if period in pv:
            old_pv = pv[period]
            bucket["pv_source"] = "legacy_import"
            if old_pv is None:
                bucket["pv_quality"] = "unknown"
            else:
                field = "eDag" if grain == "day" else "eMaand"
                value = _decimal(old_pv[field]) * pv_factor
                if value >= 0:
                    bucket["pv_generated_kwh"] = value
                    bucket["pv_quality"] = "estimated"
                else:
                    bucket["pv_quality"] = "unknown"
                    warnings.append("{}: negatieve oude PV-waarde".format(period))

        result.append(bucket)

    return result, warnings
