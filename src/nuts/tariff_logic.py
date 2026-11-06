"""Validation and dated writes for the tariff administration page.

All quoted amounts and units are kept as entered. A later effective date
closes the previous contract or rule for the same stream.
"""

import calendar
import re
from datetime import date
from decimal import Decimal, InvalidOperation


COMMODITIES = ("electricity", "gas", "water")
SOURCE_KINDS = ("network_operator", "government")
COMPONENTS = (
    "supply", "energy_tax", "fixed_supply", "tax_credit",
    "export_compensation", "feedin_cost", "network",
)
FLOWS = ("import", "export", "none")
REGISTERS = ("low", "high", "all")
BASES = ("per_kwh", "per_m3", "per_day", "per_month", "per_year")
BANDS = ("", "settlement_export_kwh", "settlement_import_kwh", "calendar_gas_m3")
POLICIES = ("saldering", "separate_import_export")
CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class InputError(ValueError):
    pass


def pick(form, key):
    return str(form.get(key, "")).strip()


def choose(value, allowed, label):
    if value not in allowed:
        raise InputError("Ongeldige keuze voor {}".format(label))
    return value


def iso_date(value, label, optional=False):
    if optional and not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InputError("{} moet een geldige datum zijn".format(label)) from exc


def amount(value, label, optional=False):
    if optional and not value:
        return None
    try:
        number = Decimal(value.replace(",", "."))
    except InvalidOperation as exc:
        raise InputError("{} moet een getal zijn".format(label)) from exc
    if not number.is_finite() or abs(number) >= Decimal("10000000"):
        raise InputError("{} ligt buiten het bereik".format(label))
    if number.as_tuple().exponent < -8:
        raise InputError("{} heeft meer dan acht decimalen".format(label))
    return number


def positive_id(value, label):
    try:
        result = int(value)
    except ValueError as exc:
        raise InputError("{} moet een nummer zijn".format(label)) from exc
    if result < 1:
        raise InputError("{} moet een nummer zijn".format(label))
    return result


def contract_form(form):
    commodity = choose(pick(form, "commodity"), COMMODITIES, "nutssoort")
    supplier = pick(form, "supplier")
    if not supplier or len(supplier) > 120:
        raise InputError("Vul een leverancier van maximaal 120 tekens in")
    description = pick(form, "description")
    if len(description) > 200:
        raise InputError("Omschrijving is te lang")
    start = iso_date(pick(form, "valid_from"), "Ingangsdatum")
    return commodity, supplier, description or None, start


def source_form(form):
    commodity = choose(pick(form, "commodity"), COMMODITIES, "nutssoort")
    kind = choose(pick(form, "source_kind"), SOURCE_KINDS, "bron")
    name = pick(form, "source_name")
    if not name or len(name) > 120:
        raise InputError("Vul een bronnaam van maximaal 120 tekens in")
    return commodity, kind, name


def settlement_form(form):
    contract_id = positive_id(pick(form, "contract_id"), "Stroomcontract")
    start = iso_date(pick(form, "starts_on"), "Begindatum")
    end = iso_date(pick(form, "ends_on"), "Einddatum")
    if end <= start:
        raise InputError("Einddatum moet na de begindatum liggen")
    policy = choose(pick(form, "electricity_policy"), POLICIES,
                    "afrekenwijze")
    return contract_id, start, end, policy


def rule_form(form):
    source_id = positive_id(pick(form, "source_id"), "Bron")
    code = pick(form, "code").lower().replace(" ", "_").replace("-", "_")
    if not CODE.fullmatch(code):
        raise InputError("Herkenningsnaam: kleine letters, cijfers en _; begin met een letter")
    component = choose(pick(form, "component"), COMPONENTS, "onderdeel")
    flow = choose(pick(form, "flow"), FLOWS, "richting")
    register = choose(pick(form, "register_code"), REGISTERS, "telwerk")
    basis = choose(pick(form, "basis"), BASES, "eenheid")
    quoted = amount(pick(form, "amount_eur"), "Bedrag")
    includes_vat = choose(pick(form, "includes_vat"), ("0", "1"), "btw") == "1"
    vat_rate = amount(pick(form, "vat_rate") or "0", "Btw-tarief")
    if vat_rate > 1 and vat_rate <= 100:
        vat_rate /= 100
    if vat_rate < 0 or vat_rate > 1:
        raise InputError("Btw-tarief moet tussen 0 en 1 liggen")
    if vat_rate.as_tuple().exponent < -4:
        raise InputError("Btw-tarief heeft maximaal vier decimalen")
    start = iso_date(pick(form, "valid_from"), "Ingangsdatum")
    end = iso_date(pick(form, "valid_until"), "Einddatum", optional=True)
    if end is not None and end <= start:
        raise InputError("Einddatum moet na de ingangsdatum liggen")
    band = choose(pick(form, "band_quantity"), BANDS, "verbruiksband") or None
    lower = amount(pick(form, "band_from"), "Band vanaf", optional=True)
    upper = amount(pick(form, "band_until"), "Band tot", optional=True)
    if any(value is not None and value.as_tuple().exponent < -3
           for value in (lower, upper)):
        raise InputError("Bandgrenzen hebben maximaal drie decimalen")
    if band is None and (lower is not None or upper is not None):
        raise InputError("Kies een band bij de grenswaarden")
    if band is not None and (lower is None or lower < 0):
        raise InputError("Vul een niet-negatieve ondergrens in")
    if upper is not None and upper <= lower:
        raise InputError("Bovengrens moet boven de ondergrens liggen")
    notes = pick(form, "notes")
    if len(notes) > 255:
        raise InputError("Toelichting is te lang")
    return dict(source_id=source_id, code=code, component=component, flow=flow,
                register_code=register, basis=basis, amount_eur=quoted,
                includes_vat=includes_vat, vat_rate=vat_rate,
                valid_from=start, valid_until=end, band_quantity=band,
                band_from=lower, band_until=upper, notes=notes or None)


def daily_equivalent(quoted, basis, day):
    quoted = Decimal(str(quoted))
    if basis == "per_day":
        return quoted
    if basis == "per_month":
        return quoted / Decimal(calendar.monthrange(day.year, day.month)[1])
    if basis == "per_year":
        return quoted / Decimal(366 if calendar.isleap(day.year) else 365)
    return None


def effective_amount(rule):
    """Return the amount including VAT, without changing the stored quote."""
    quoted = Decimal(str(rule["amount_eur"]))
    if not rule["includes_vat"]:
        quoted *= 1 + Decimal(str(rule["vat_rate"]))
    return quoted


def current_rate_summary(active_rules, day):
    """Preview certain active rates; settlement bands stay outside totals."""
    result = {
        commodity: {"low": Decimal(0), "high": Decimal(0),
                    "variable_m3": Decimal(0), "fixed_supply_day": Decimal(0),
                    "tax_credit_day": Decimal(0), "network_day": Decimal(0)}
        for commodity in COMMODITIES
    }
    conditional = []
    for rule in active_rules:
        if rule["band_quantity"] is not None:
            conditional.append(rule)
            continue
        target = result[rule["commodity"]]
        value = effective_amount(rule)
        if rule["component"] in ("supply", "energy_tax"):
            if rule["commodity"] == "electricity":
                for register in ("low", "high"):
                    if rule["register_code"] in ("all", register):
                        target[register] += value
            else:
                target["variable_m3"] += value
        elif rule["component"] in ("fixed_supply", "tax_credit", "network"):
            daily = daily_equivalent(value, rule["basis"], day)
            if daily is not None:
                target[rule["component"] + "_day"] += daily
        else:
            conditional.append(rule)
    return result, conditional


def overlaps(start_a, end_a, start_b, end_b):
    return (end_a is None or start_b < end_a) and (end_b is None or start_a < end_b)


def rows(connection, sql, params=()):
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(sql, params)
        return cursor.fetchall()
    finally:
        cursor.close()


def one(connection, sql, params=()):
    result = rows(connection, sql, params)
    return result[0] if result else None


def execute(connection, sql, params=()):
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        return cursor.lastrowid
    finally:
        cursor.close()


def save_contract(connection, form):
    commodity, supplier, description, start = contract_form(form)
    existing = rows(connection, """
        SELECT id, valid_from, valid_until FROM contract
        WHERE commodity=%s ORDER BY valid_from FOR UPDATE
    """, (commodity,))
    if any(row["valid_from"] == start for row in existing):
        raise InputError("Er is al een contract voor deze nutssoort op deze datum")
    previous = [row for row in existing if row["valid_from"] < start]
    following = [row for row in existing if row["valid_from"] > start]
    if previous:
        old = previous[-1]
        if old["valid_until"] is None or old["valid_until"] > start:
            if commodity == "electricity":
                periods = rows(connection, """
                    SELECT id, starts_on, ends_on FROM settlement_period
                    WHERE contract_id=%s AND ends_on>%s FOR UPDATE
                """, (old["id"], start))
                if any(period["starts_on"] >= start for period in periods):
                    raise InputError("Er bestaat een latere afrekenperiode bij het vorige contract")
                for period in periods:
                    execute(connection, """
                        UPDATE settlement_period SET ends_on=%s WHERE id=%s
                    """, (start, period["id"]))
            execute(connection, "UPDATE contract SET valid_until=%s WHERE id=%s",
                    (start, old["id"]))
    end = following[0]["valid_from"] if following else None
    contract_id = execute(connection, """
        INSERT INTO contract (commodity, supplier, description, valid_from, valid_until)
        VALUES (%s,%s,%s,%s,%s)
    """, (commodity, supplier, description, start, end))
    execute(connection, """
        INSERT INTO tariff_source (commodity, source_kind, source_name, contract_id)
        VALUES (%s,'supplier',%s,%s)
    """, (commodity, supplier, contract_id))


def save_source(connection, form):
    commodity, kind, name = source_form(form)
    if one(connection, """
        SELECT id FROM tariff_source WHERE commodity=%s AND source_kind=%s
          AND source_name=%s AND contract_id IS NULL
    """, (commodity, kind, name)):
        raise InputError("Deze bron bestaat al")
    execute(connection, """
        INSERT INTO tariff_source (commodity, source_kind, source_name)
        VALUES (%s,%s,%s)
    """, (commodity, kind, name))


def save_settlement(connection, form):
    contract_id, start, end, policy = settlement_form(form)
    contract = one(connection, """
        SELECT commodity, valid_from, valid_until FROM contract
        WHERE id=%s FOR UPDATE
    """, (contract_id,))
    if contract is None or contract["commodity"] != "electricity":
        raise InputError("Kies een stroomcontract")
    if start < contract["valid_from"] or (
            contract["valid_until"] is not None and end > contract["valid_until"]):
        raise InputError("Afrekenperiode valt buiten het contract")
    existing = rows(connection, """
        SELECT starts_on, ends_on FROM settlement_period
        WHERE contract_id=%s FOR UPDATE
    """, (contract_id,))
    if any(overlaps(start, end, row["starts_on"], row["ends_on"])
           for row in existing):
        raise InputError("Afrekenperiode overlapt een bestaande periode")
    execute(connection, """
        INSERT INTO settlement_period
        (contract_id, starts_on, ends_on, electricity_policy)
        VALUES (%s,%s,%s,%s)
    """, (contract_id, start, end, policy))


def save_rule(connection, form, actor):
    rule = rule_form(form)
    source = one(connection, """
        SELECT s.commodity, s.source_kind, c.valid_from AS contract_from,
               c.valid_until AS contract_until
        FROM tariff_source s LEFT JOIN contract c ON c.id=s.contract_id
        WHERE s.id=%s
    """, (rule["source_id"],))
    if source is None:
        raise InputError("Tariefbron bestaat niet")
    commodity, kind, component = source["commodity"], source["source_kind"], rule["component"]
    if commodity != "electricity" and rule["register_code"] != "all":
        raise InputError("Dal/normaal is alleen voor stroom")
    if component in ("fixed_supply", "tax_credit", "network", "feedin_cost") and \
            rule["register_code"] != "all":
        raise InputError("Dit vaste of terugleveronderdeel gebruikt beide telwerken")
    source_for_component = {
        "supply": "supplier", "fixed_supply": "supplier",
        "export_compensation": "supplier", "feedin_cost": "supplier",
        "energy_tax": "government", "tax_credit": "government",
        "network": "network_operator",
    }
    if kind != source_for_component[component]:
        raise InputError("Dit onderdeel hoort bij een andere tariefbron")
    if component in ("export_compensation", "feedin_cost") and commodity != "electricity":
        raise InputError("Teruglevering is alleen voor stroom")
    if component in ("fixed_supply", "tax_credit", "network") and rule["flow"] != "none":
        raise InputError("Een vast bedrag heeft geen afname- of terugleverrichting")
    if component in ("supply", "energy_tax") and rule["flow"] != "import":
        raise InputError(
            "Onderdeel staat op Variabele levering of Energiebelasting. "
            "Gebruik voor teruglevering het korte formulier "
            "'Teruglevering boven verbruik' bij stap 4")
    if component == "export_compensation" and rule["flow"] != "export":
        raise InputError("Terugleververgoeding hoort bij teruglevering")
    if component == "feedin_cost" and rule["flow"] != "export":
        raise InputError("Terugleverkosten horen bij teruglevering")
    if component in ("tax_credit", "export_compensation") and rule["amount_eur"] > 0:
        raise InputError("Een vergoeding hoort als negatief bedrag te worden ingevoerd")
    if component not in ("tax_credit", "export_compensation") and rule["amount_eur"] < 0:
        raise InputError("Kosten horen als positief bedrag te worden ingevoerd")
    if source["source_kind"] == "supplier":
        if rule["valid_from"] < source["contract_from"] or (
                source["contract_until"] is not None and
                rule["valid_from"] >= source["contract_until"]):
            raise InputError("Tarief begint buiten het contract")
    if rule["component"] in ("supply", "energy_tax", "export_compensation"):
        required = "per_kwh" if source["commodity"] == "electricity" else "per_m3"
        if rule["basis"] != required:
            raise InputError("Dit variabele tarief hoort {} te zijn".format(required))
    if rule["component"] in ("fixed_supply", "tax_credit", "network") and \
            rule["basis"] not in ("per_day", "per_month", "per_year"):
        raise InputError("Dit vaste tarief hoort per dag, maand of jaar te zijn")
    if component == "feedin_cost" and rule["basis"] not in (
            "per_kwh", "per_day", "per_month", "per_year"):
        raise InputError("Terugleverkosten horen per kWh, dag, maand of jaar te zijn")
    if rule["band_quantity"] == "calendar_gas_m3" and commodity != "gas":
        raise InputError("De gasband hoort alleen bij gas")
    if rule["band_quantity"] in ("settlement_export_kwh", "settlement_import_kwh") \
            and commodity != "electricity":
        raise InputError("De kWh-band hoort alleen bij stroom")
    latest = one(connection, """
        SELECT COALESCE(MAX(revision),0) AS revision FROM tariff_rule
        WHERE tariff_source_id=%s AND code=%s AND valid_from=%s
    """, (rule["source_id"], rule["code"], rule["valid_from"]))
    execute(connection, """
        INSERT INTO tariff_rule
        (tariff_source_id, code, component, flow, register_code, basis,
         amount_eur, includes_vat, vat_rate, valid_from, valid_until,
         band_quantity, band_from, band_until, status, revision,
         entered_at_utc, entered_by, notes)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                'draft',%s,UTC_TIMESTAMP(3),%s,%s)
    """, (rule["source_id"], rule["code"], rule["component"], rule["flow"],
          rule["register_code"], rule["basis"], rule["amount_eur"],
          rule["includes_vat"], rule["vat_rate"], rule["valid_from"],
          rule["valid_until"], rule["band_quantity"], rule["band_from"],
          rule["band_until"], latest["revision"] + 1, actor[:80], rule["notes"]))


def activate_rule(connection, rule_id):
    rule = one(connection, "SELECT * FROM tariff_rule WHERE id=%s FOR UPDATE",
               (rule_id,))
    if rule is None or rule["status"] != "draft":
        raise InputError("Alleen een bestaand concept kan actief worden")
    equivalents = rows(connection, """
        SELECT id, code, flow, register_code, basis, band_quantity,
               band_from, band_until, valid_from, valid_until
        FROM tariff_rule
        WHERE tariff_source_id=%s AND component=%s AND status='active'
        FOR UPDATE
    """, (rule["tariff_source_id"], rule["component"]))
    for other in equivalents:
        same_slot = all(other[key] == rule[key] for key in (
            "flow", "band_quantity", "band_from", "band_until"))
        same_slot = same_slot and (other["register_code"] == rule["register_code"]
                                   or "all" in (other["register_code"],
                                                rule["register_code"]))
        if rule["component"] == "feedin_cost":
            same_slot = same_slot and other["basis"] == rule["basis"]
        if same_slot and other["code"] != rule["code"] and overlaps(
                other["valid_from"], other["valid_until"],
                rule["valid_from"], rule["valid_until"]):
            raise InputError("Er is al een actieve regel voor dit onderdeel en telwerk")
    existing = rows(connection, """
        SELECT id, valid_from, valid_until FROM tariff_rule
        WHERE tariff_source_id=%s AND code=%s AND status='active'
        ORDER BY valid_from FOR UPDATE
    """, (rule["tariff_source_id"], rule["code"]))
    replaced = None
    for old in existing:
        if not overlaps(old["valid_from"], old["valid_until"],
                        rule["valid_from"], rule["valid_until"]):
            continue
        if old["valid_from"] == rule["valid_from"]:
            execute(connection, "UPDATE tariff_rule SET status='superseded' WHERE id=%s",
                    (old["id"],))
            replaced = old["id"]
        elif old["valid_from"] < rule["valid_from"]:
            execute(connection, "UPDATE tariff_rule SET valid_until=%s WHERE id=%s",
                    (rule["valid_from"], old["id"]))
        else:
            raise InputError("Een latere actieve regel met dezelfde naam bestaat al")
    execute(connection, """
        UPDATE tariff_rule SET status='active', supersedes_rule_id=%s WHERE id=%s
    """, (replaced, rule_id))
