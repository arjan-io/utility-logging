"""Recurring advance amounts linked to one or two supply contracts.

An advance is a scheduled amount, not proof that a bank payment occurred.
One electricity-and-gas advance is stored once and linked to both contracts.
"""

from decimal import Decimal

from . import tariff_logic as db


COMMODITIES = ("electricity", "gas", "water")
CADENCES = ("monthly", "quarterly")


def parse_form(form):
    contracts = {}
    for commodity in COMMODITIES:
        raw = db.pick(form, commodity + "_contract_id")
        if raw:
            contracts[commodity] = db.positive_id(raw, "Contract " + commodity)
    if not contracts or ("water" in contracts and len(contracts) > 1):
        raise db.InputError("Kies één contract, of stroom en gas samen")
    if len(set(contracts.values())) != len(contracts):
        raise db.InputError("Kies verschillende contracten voor stroom en gas")
    quoted = db.amount(db.pick(form, "amount_eur"), "Voorschot")
    if quoted <= 0 or quoted.as_tuple().exponent < -2:
        raise db.InputError("Voorschot moet positief zijn en maximaal twee decimalen hebben")
    cadence = db.choose(db.pick(form, "cadence"), CADENCES, "betaalritme")
    start = db.iso_date(db.pick(form, "valid_from"), "Begindatum")
    end = db.iso_date(db.pick(form, "valid_until"), "Einddatum", optional=True)
    first_due = db.iso_date(db.pick(form, "first_due_on"), "Eerste betaaldatum")
    if end is not None and end <= start:
        raise db.InputError("Einddatum moet na de begindatum liggen")
    if first_due < start or (end is not None and first_due >= end):
        raise db.InputError("Eerste betaaldatum moet binnen de geldigheidsperiode vallen")
    notes = db.pick(form, "notes")
    if len(notes) > 255:
        raise db.InputError("Toelichting is te lang")
    return dict(contracts=contracts, amount_eur=quoted, cadence=cadence,
                valid_from=start, valid_until=end, first_due_on=first_due,
                notes=notes or None)


def overlaps(start_a, end_a, start_b, end_b):
    return (end_a is None or start_b < end_a) and \
        (end_b is None or start_a < end_b)


def save_schedule(connection, form):
    plan = parse_form(form)
    ids = tuple(plan["contracts"].values())
    placeholders = ",".join(["%s"] * len(ids))
    contracts = db.rows(connection, """
        SELECT id, commodity, valid_from, valid_until FROM contract
        WHERE id IN ({}) FOR UPDATE
    """.format(placeholders), ids)
    found = {row["commodity"]: row for row in contracts}
    if any(found.get(commodity, {}).get("id") != contract_id
           for commodity, contract_id in plan["contracts"].items()):
        raise db.InputError("Kies bestaande contracten van de juiste nutssoort")
    if any(plan["valid_from"] < row["valid_from"] for row in contracts):
        raise db.InputError("Voorschot begint vóór een gekoppeld contract")
    contract_ends = [row["valid_until"] for row in contracts
                     if row["valid_until"] is not None]
    if contract_ends:
        last_day = min(contract_ends)
        if plan["valid_until"] is None or plan["valid_until"] > last_day:
            plan["valid_until"] = last_day
    if plan["valid_until"] is not None and (
            plan["valid_until"] <= plan["valid_from"] or
            plan["first_due_on"] >= plan["valid_until"]):
        raise db.InputError("Voorschot valt buiten een gekoppeld contract")
    matched = db.rows(connection, """
        SELECT s.id, s.valid_from, s.valid_until, s.first_due_on
        FROM advance_schedule s
        JOIN advance_schedule_contract l ON l.schedule_id=s.id
        WHERE s.status='active' AND l.contract_id IN ({})
        FOR UPDATE
    """.format(placeholders), ids)
    active = {row["id"]: row for row in matched}
    if active:
        previous_ids = tuple(active)
        links = db.rows(connection, """
            SELECT schedule_id, contract_id FROM advance_schedule_contract
            WHERE schedule_id IN ({})
        """.format(",".join(["%s"] * len(previous_ids))), previous_ids)
        scopes = {schedule_id: set() for schedule_id in previous_ids}
        for link in links:
            scopes[link["schedule_id"]].add(link["contract_id"])
        for schedule_id, old in active.items():
            if not overlaps(old["valid_from"], old["valid_until"],
                            plan["valid_from"], plan["valid_until"]):
                continue
            if scopes[schedule_id] != set(ids):
                raise db.InputError(
                    "Er loopt al een gezamenlijk of apart voorschot voor dit contract. "
                    "Verwerp dat eerst of kies dezelfde contracten")
            if old["valid_from"] >= plan["valid_from"]:
                raise db.InputError("Er bestaat al een voorschot vanaf deze of een latere datum")
            if old["first_due_on"] >= plan["valid_from"]:
                raise db.InputError("Nieuw voorschot begint vóór de eerste betaling van het vorige")
            db.execute(connection, """
                UPDATE advance_schedule SET valid_until=%s WHERE id=%s
            """, (plan["valid_from"], schedule_id))
    schedule_id = db.execute(connection, """
        INSERT INTO advance_schedule
        (amount_eur, cadence, valid_from, valid_until, first_due_on,
         status, notes, entered_at_utc)
        VALUES (%s,%s,%s,%s,%s,'active',%s,UTC_TIMESTAMP(3))
    """, (plan["amount_eur"], plan["cadence"], plan["valid_from"],
          plan["valid_until"], plan["first_due_on"], plan["notes"]))
    for commodity, contract_id in plan["contracts"].items():
        db.execute(connection, """
            INSERT INTO advance_schedule_contract (schedule_id, commodity, contract_id)
            VALUES (%s,%s,%s)
        """, (schedule_id, commodity, contract_id))


def void_schedule(connection, schedule_id):
    row = db.one(connection, """
        SELECT status FROM advance_schedule WHERE id=%s FOR UPDATE
    """, (schedule_id,))
    if row is None or row["status"] != "active":
        raise db.InputError("Alleen een actief voorschot kan worden verworpen")
    db.execute(connection, "UPDATE advance_schedule SET status='void' WHERE id=%s",
               (schedule_id,))
