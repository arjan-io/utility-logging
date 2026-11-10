"""Server-rendered page for recurring contract advance amounts."""

import html

from . import tariff_logic as db


NAMES = {"electricity": "Stroom", "gas": "Gas", "water": "Water"}


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def page(connection, csrf, message=""):
    contracts = db.rows(connection, """
        SELECT id, commodity, supplier, valid_from, valid_until
        FROM contract ORDER BY commodity, valid_from DESC
    """)
    schedules = db.rows(connection, """
        SELECT s.id, s.amount_eur, s.cadence, s.valid_from, s.valid_until,
               s.first_due_on, s.status, s.notes, x.contract_names
        FROM advance_schedule s JOIN (
            SELECT l.schedule_id,
                   GROUP_CONCAT(CONCAT(l.commodity, ': ', c.supplier)
                                ORDER BY l.commodity SEPARATOR ' + ') AS contract_names
            FROM advance_schedule_contract l
            JOIN contract c ON c.id=l.contract_id
            GROUP BY l.schedule_id
        ) x ON x.schedule_id=s.id
        ORDER BY s.valid_from DESC, s.id DESC LIMIT 100
    """)
    contract_options = {}
    for commodity in NAMES:
        contract_options[commodity] = '<option value="">Geen</option>' + "".join(
            '<option value="{}">{} · vanaf {}</option>'.format(
                row["id"], esc(row["supplier"]), esc(row["valid_from"]))
            for row in contracts if row["commodity"] == commodity)
    listing = []
    for row in schedules:
        action = ""
        if row["status"] == "active":
            action = ('<form method="post" action="/advance-void">'
                      '<input type="hidden" name="csrf" value="{}">'
                      '<input type="hidden" name="id" value="{}">'
                      '<button class="muted">Verwerpen</button></form>').format(
                          esc(csrf), row["id"])
        listing.append(
            "<tr><td>{}</td><td>€{} per {}</td><td>{} – {}</td>"
            "<td>{}</td><td>{}</td><td>{}</td></tr>".format(
                esc(row["contract_names"]), esc(row["amount_eur"]),
                "maand" if row["cadence"] == "monthly" else "kwartaal",
                esc(row["valid_from"]), esc(row["valid_until"] or "volgend voorschot"),
                esc(row["first_due_on"]),
                "actief" if row["status"] == "active" else "verworpen", action))
    notice = '<p class="notice">{}</p>'.format(esc(message)) if message else ""
    return """<!doctype html><html lang="nl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Voorschotten</title><style>
body{{font:16px system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;color:#183242}}
h1,h2{{color:#125265}}section{{border:1px solid #c6d5dd;border-radius:12px;padding:1rem;margin:1rem 0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:0 1rem}}
.field{{min-width:0;margin:.7rem 0}}label{{display:block;margin-bottom:.35rem;font-weight:600}}
input,select{{display:block;width:100%;min-height:2.8rem;padding:.55rem;box-sizing:border-box;
font:inherit;background:white;color:#183242;border:1px solid #8296a0;border-radius:6px}}
input:focus,select:focus{{outline:3px solid #7bc9c4;outline-offset:2px}}
button{{background:#08736f;color:white;border:0;border-radius:6px;padding:.6rem 1rem;cursor:pointer}}
.muted{{background:#687b83}}table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #d9e1e5;padding:.6rem;text-align:left;vertical-align:top}}
.scroll{{overflow-x:auto}}.hint{{color:#425863}}.notice{{background:#e7f4ec;padding:.8rem;border-radius:6px}}
</style><main><nav><a href="/">← Tarieven</a> · <a href="/nota">Verwachte nota</a></nav><h1>Voorschotten</h1>{notice}
<p class="hint">Dit zijn afgesproken periodieke bedragen, geen geregistreerde bankbetalingen.
Eén gezamenlijk voorschot voor stroom en gas wordt één keer opgeslagen en aan beide contracten gekoppeld.
Water kan alleen als apart voorschot worden ingevoerd.</p>
<section><h2>Nieuw voorschot</h2><p class="hint">Kies stroom, gas, water of stroom én gas.
Bij een wijziging met dezelfde contracten sluit het nieuwe voorschot het vorige op zijn ingangsdatum.
De einddatum mag leeg blijven; een contracteinddatum begrenst het voorschot automatisch.</p>
<form method="post" action="/advance"><input type="hidden" name="csrf" value="{csrf}">
<div class="grid">
<div class="field"><label for="advance-electricity">Stroomcontract</label><select id="advance-electricity" name="electricity_contract_id">{electricity}</select></div>
<div class="field"><label for="advance-gas">Gascontract</label><select id="advance-gas" name="gas_contract_id">{gas}</select></div>
<div class="field"><label for="advance-water">Watercontract</label><select id="advance-water" name="water_contract_id">{water}</select></div>
<div class="field"><label for="advance-amount">Bedrag per betaling in euro</label><input id="advance-amount" name="amount_eur" inputmode="decimal" placeholder="150,00" required></div>
<div class="field"><label for="advance-cadence">Hoe vaak wordt dit bedrag betaald?</label><select id="advance-cadence" name="cadence"><option value="monthly">Elke maand</option><option value="quarterly">Elk kwartaal</option></select></div>
<div class="field"><label for="advance-from">Ingangsdatum van dit bedrag</label><input id="advance-from" name="valid_from" type="date" required></div>
<div class="field"><label for="advance-due">Eerste betaaldatum</label><input id="advance-due" name="first_due_on" type="date" required></div>
<div class="field"><label for="advance-until">Eerste dag na deze regeling (optioneel)</label><input id="advance-until" name="valid_until" type="date"></div>
<div class="field"><label for="advance-notes">Toelichting (optioneel)</label><input id="advance-notes" name="notes" maxlength="255"></div>
</div><button>Voorschot opslaan</button></form></section>
<section><h2>Ingevoerde voorschotten</h2><div class="scroll"><table>
<tr><th>Contracten</th><th>Bedrag</th><th>Geldig</th><th>Eerste betaaldatum</th><th>Status</th><th></th></tr>
{listing}</table></div></section></main></html>""".format(
        notice=notice, csrf=esc(csrf), electricity=contract_options["electricity"],
        gas=contract_options["gas"], water=contract_options["water"],
        listing="".join(listing))
