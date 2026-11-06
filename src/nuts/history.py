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
    if grain == "day":
        indexed = {}
        duplicates = {}
        for row in rows:
            period = period_key(row["datum"], "day")
            if period in indexed:
                duplicates.setdefault(period, [indexed[period]]).append(row)
            indexed[period] = row
        for period, candidates in sorted(duplicates.items()):
            warnings.append(
                "{}: {} oude PV-dagregels (ids {}); laatste regel id {} "
                "voorlopig gebruikt, controle vereist".format(
                    period, len(candidates),
                    ", ".join(str(row.get("id")) for row in candidates),
                    indexed[period].get("id")))
        return indexed
    indexed = {}
    last_value = {}
    reported = set()
    for row in rows:
        day = as_date(row["datum"])
        period = day.replace(day=1)
        value = _decimal(row["eMaand"])
        if period in last_value and value < last_value[period][0] and period not in reported:
            previous_value, previous_id = last_value[period]
            warnings.append(
                "{}: PV-maandstand daalt van {} (id {}) naar {} (id {}); "
                "laatste maandstand voorlopig gebruikt, controle vereist".format(
                    period, previous_value, previous_id, value, row.get("id")))
            reported.add(period)
        last_value[period] = (value, row.get("id"))
        indexed[period] = row
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

        old_pv = pv.get(period)
        if old_pv is not None:
            field = "eDag" if grain == "day" else "eMaand"
            value = _decimal(old_pv[field]) * pv_factor
            bucket["pv_source"] = "legacy_import"
            if value >= 0:
                bucket["pv_generated_kwh"] = value
                bucket["pv_quality"] = "estimated"
            else:
                bucket["pv_quality"] = "unknown"
                warnings.append("{}: negatieve oude PV-waarde".format(period))

        result.append(bucket)

    return result, warnings
