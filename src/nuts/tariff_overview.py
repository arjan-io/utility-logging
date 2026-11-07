"""Small, separate tariff pages for electricity, gas, and water."""

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from . import tariff_logic as logic


PATHS = {"electricity": "/stroom", "gas": "/gas", "water": "/water"}
NAMES = {"electricity": "Stroom", "gas": "Gas", "water": "Water"}
SOURCE_NAMES = {"supplier": "Leverancier", "government": "Overheid",
                "network_operator": "Netbeheerder"}
PARTS = {"supply": "Variabele levering", "fixed_supply": "Vaste levering",
         "energy_tax": "Energiebelasting", "tax_credit": "Vermindering energiebelasting",
         "network": "Netbeheerkosten", "export_compensation": "Terugleververgoeding",
         "feedin_cost": "Terugleverkosten"}
UNITS = {"per_kwh": "per kWh", "per_m3": "per m³", "per_day": "per dag",
         "per_month": "per maand", "per_year": "per jaar"}
FLOWS = {"import": "Afname", "export": "Teruglevering", "none": "Vast bedrag"}
REGISTERS = {"low": "Dal", "high": "Normaal", "all": "Beide / algemeen"}
def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def part_name(component, commodity):
    if commodity == "water":
        return {"supply": "Drinkwaterverbruik", "fixed_supply": "Vastrecht drinkwater",
                "energy_tax": "Belasting op leidingwater"}.get(component, PARTS[component])
    return PARTS[component]


STYLE = """<style>
body{font:16px system-ui,sans-serif;max-width:1050px;margin:2rem auto;padding:0 1rem;color:#183242}
h1,h2{color:#125265}nav a{margin-right:.8rem}section,details{border:1px solid #c6d5dd;border-radius:10px;padding:1rem;margin:1rem 0}
summary{cursor:pointer;font-weight:600}table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid #d9e1e5;padding:.6rem;text-align:left;vertical-align:top}
td small{display:block;color:#425863}.scroll{overflow-x:auto}.grid,.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.8rem}
.card{display:block;padding:1.2rem;background:#e7f4ec;border-radius:10px;color:#125265;text-decoration:none;font-size:1.25rem;font-weight:600}
.field{min-width:0;margin:.5rem 0}label{display:block;margin-bottom:.3rem;font-weight:600}
input,select{display:block;width:100%;min-height:2.7rem;padding:.5rem;box-sizing:border-box;font:inherit;background:#fff;color:#183242;border:1px solid #8296a0;border-radius:6px}
input:focus,select:focus,a:focus,button:focus{outline:3px solid #7bc9c4;outline-offset:2px}
button{background:#08736f;color:white;border:0;border-radius:6px;padding:.6rem 1rem;cursor:pointer}
.muted{background:#687b83}td form{display:inline-block;margin:.15rem}
.active td{background:#edf7ef}.draft td{background:#fff7da}.inactive td{background:#ffe1e3}
.hint{color:#425863}.notice{background:#e7f4ec;padding:.8rem;border-radius:6px}
</style>"""


def document(title, body):
    return ('<!doctype html><html lang="nl"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>{}</title>{}<main>{}</main></html>'.format(esc(title), STYLE, body))


def nav():
    return '<nav><a href="/">Overzicht</a><a href="/stroom">Stroom</a><a href="/gas">Gas</a><a href="/water">Water</a><a href="/advances">Voorschotten</a></nav>'


def home_page():
    cards = ''.join('<a class="card" href="{}">{} →</a>'.format(PATHS[key], NAMES[key])
                    for key in PATHS)
    return document("Tarieven", nav() + '<h1>Tarieven</h1><p>Kies de nutssoort die je wilt bekijken of aanpassen.</p>'
                    '<div class="cards">{}</div>'.format(cards))


def _rule_rows(connection, commodity, archive):
    today = datetime.now(ZoneInfo("Europe/Amsterdam")).date()
    status = ("(r.status='superseded' OR (r.status='active' AND r.valid_until<=%s))"
              if archive else
              "(r.status='draft' OR (r.status='active' AND "
              "(r.valid_until IS NULL OR r.valid_until>%s)))")
    return logic.rows(connection, """
        SELECT r.id, r.code, r.component, r.flow, r.register_code, r.basis,
               r.amount_eur, r.includes_vat, r.vat_rate, r.valid_from,
               r.valid_until, r.status, r.band_quantity, r.band_from,
               r.band_until, r.notes, s.source_name, s.source_kind
        FROM tariff_rule r JOIN tariff_source s ON s.id=r.tariff_source_id
        WHERE s.commodity=%s AND {} ORDER BY r.valid_from DESC, r.id DESC
        LIMIT 500
    """.format(status), (commodity, today))


def _rule_row(rule, commodity, csrf, archive):
    status = rule["status"]
    notes = rule["notes"] or ""
    marked_inactive = notes.startswith(("[ingetrokken]", "[verworpen]"))
    label = ("ingetrokken" if notes.startswith("[ingetrokken]") else
             "verworpen concept" if notes.startswith("[verworpen]") else
             "afgelopen" if archive and status == "active" else
             "vervangen" if archive else "actief" if status == "active" else "concept")
    css = "inactive" if marked_inactive else "" if archive else "active" if status == "active" else "draft"
    band = ""
    if rule["band_quantity"]:
        band = " · jaar-teruglevering vanaf {}{} kWh".format(
            esc(rule["band_from"]),
            " tot {}".format(esc(rule["band_until"])) if rule["band_until"] else "")
    vat = ("incl. btw" if rule["includes_vat"] else
           "excl. btw; {}%".format(format((rule["vat_rate"] * 100).normalize(), "f")))
    actions = ""
    path = PATHS[commodity]
    if status == "draft" and not archive:
        actions = ('<form method="post" action="/activate"><input type="hidden" name="csrf" value="{}">'
                   '<input type="hidden" name="id" value="{}"><input type="hidden" name="return_to" value="{}">'
                   '<button>Activeren</button></form>'
                   '<form method="post" action="/discard"><input type="hidden" name="csrf" value="{}">'
                   '<input type="hidden" name="id" value="{}"><input type="hidden" name="return_to" value="{}">'
                   '<button class="muted">Verwerpen</button></form>').format(
                       esc(csrf), rule["id"], path, esc(csrf), rule["id"], path)
    elif status == "active" and not archive:
        actions = '<a href="/withdraw?id={}">Intrekken</a>'.format(rule["id"])
    return ('<tr class="{}"><td>{}<small>{} · {}</small></td>'
            '<td>{}<small>{} · {}{}</small></td><td>€{} {}<small>{}</small></td>'
            '<td>{} – {}</td><td>{}</td><td>{}</td></tr>').format(
                css, esc(rule["source_name"]), esc(SOURCE_NAMES[rule["source_kind"]]),
                esc(rule["code"]), esc(part_name(rule["component"], commodity)),
                esc(FLOWS[rule["flow"]]), esc(REGISTERS[rule["register_code"]]), band,
                esc(rule["amount_eur"]), esc(UNITS[rule["basis"]]), esc(vat),
                esc(rule["valid_from"]), esc(rule["valid_until"] or "volgende invoer"),
                esc(label), actions)


def _current_summary(connection, commodity):
    today = datetime.now(ZoneInfo("Europe/Amsterdam")).date()
    current = logic.rows(connection, """
        SELECT r.component, r.register_code, r.basis, r.amount_eur,
               r.includes_vat, r.vat_rate, r.band_quantity, s.commodity
        FROM tariff_rule r JOIN tariff_source s ON s.id=r.tariff_source_id
        LEFT JOIN contract c ON c.id=s.contract_id
        WHERE s.commodity=%s AND r.status='active' AND r.valid_from<=%s
          AND (r.valid_until IS NULL OR r.valid_until>%s)
          AND (s.source_kind<>'supplier' OR
               (c.valid_from<=%s AND (c.valid_until IS NULL OR c.valid_until>%s)))
    """, (commodity, today, today, today, today))
    if not current:
        return '<p class="hint">Nog geen actuele tarieven ingevoerd.</p>'
    totals, conditional = logic.current_rate_summary(current, today)
    values = totals[commodity]
    variable = ('Dal €{:.5f}/kWh · normaal €{:.5f}/kWh'.format(
        values["low"], values["high"]) if commodity == "electricity" else
        '€{:.5f}/m³'.format(values["variable_m3"]))
    fixed = 'Vaste levering €{:.5f}/dag'.format(values["fixed_supply_day"])
    note = (' <span class="hint">{} regel(s) met een band of afrekening staan niet in dit dagtotaal.</span>'.format(
        len(conditional)) if conditional else "")
    return '<p><strong>Variabel incl. btw:</strong> {}<br>{}{}</p>'.format(variable, fixed, note)


def commodity_page(connection, commodity, csrf, archive=False, notice=""):
    path = PATHS[commodity]
    title = NAMES[commodity]
    rules = _rule_rows(connection, commodity, archive)
    items = ''.join(_rule_row(row, commodity, csrf, archive) for row in rules)
    if not items:
        items = '<tr><td colspan="6">Geen tarieven in deze lijst.</td></tr>'
    message = '<p class="notice">{}</p>'.format(esc(notice)) if notice else ""
    heading = 'Archief {}'.format(title.lower()) if archive else 'Tarieven {}'.format(title.lower())
    content = (nav() + '<h1>{}</h1>{}'.format(esc(heading), message) +
               ('<p><a href="{}">← Actuele tarieven en concepten</a></p>'.format(path) if archive else
                '<p><a href="/new?commodity={}">+ Bedrag invoeren</a> · '
                '<a href="/archief?soort={}">Archief bekijken</a> · '
                '<a href="/setup">Uitgebreid beheer</a></p>'.format(commodity, commodity)))
    if not archive:
        content += '<section><h2>Vandaag</h2>{}</section>'.format(_current_summary(connection, commodity))
    content += ('<section><h2>{}</h2><div class="scroll"><table><tr><th>Bron</th><th>Onderdeel</th>'
                '<th>Bedrag</th><th>Periode</th><th>Status</th><th></th></tr>{}</table></div></section>').format(
                    'Ingetrokken, verworpen en vervangen' if archive else 'Actief en concept', items)
    if not archive:
        content += _setup_sections(connection, commodity, csrf)
    return document(heading, content)


def _setup_sections(connection, commodity, csrf):
    path = PATHS[commodity]
    contracts = logic.rows(connection, """
        SELECT id, supplier, valid_from, valid_until FROM contract
        WHERE commodity=%s ORDER BY valid_from DESC
    """, (commodity,))
    sources = logic.rows(connection, """
        SELECT source_kind, source_name FROM tariff_source
        WHERE commodity=%s ORDER BY source_kind, id DESC
    """, (commodity,))
    contract_rows = ''.join('<tr><td>{}</td><td>{}</td><td>{}</td></tr>'.format(
        esc(r["supplier"]), esc(r["valid_from"]), esc(r["valid_until"] or "volgend contract"))
        for r in contracts) or '<tr><td colspan="3">Nog geen contract.</td></tr>'
    source_rows = ''.join('<tr><td>{}</td><td>{}</td></tr>'.format(
        esc(SOURCE_NAMES[r["source_kind"]]), esc(r["source_name"]))
        for r in sources) or '<tr><td colspan="2">Nog geen bronnen.</td></tr>'
    result = '''<details><summary>Leverancier of contract toevoegen</summary>
<form method="post" action="/contract"><input type="hidden" name="csrf" value="{csrf}">
<input type="hidden" name="return_to" value="{path}"><input type="hidden" name="commodity" value="{commodity}">
<div class="grid"><div class="field"><label for="supplier">Leverancier</label><input id="supplier" name="supplier" required></div>
<div class="field"><label for="start">Ingangsdatum</label><input id="start" type="date" name="valid_from" required></div>
<div class="field"><label for="description">Toelichting (optioneel)</label><input id="description" name="description"></div></div>
<button>Contract opslaan</button></form>
<div class="scroll"><table><tr><th>Leverancier</th><th>Vanaf</th><th>Tot</th></tr>{contracts}</table></div></details>
<details><summary>Overheid of netbeheerder toevoegen</summary>
<form method="post" action="/source"><input type="hidden" name="csrf" value="{csrf}">
<input type="hidden" name="return_to" value="{path}"><input type="hidden" name="commodity" value="{commodity}">
<div class="grid"><div class="field"><label for="kind">Soort bron</label><select id="kind" name="source_kind">
<option value="government">Overheid / belasting</option><option value="network_operator">Netbeheerder</option></select></div>
<div class="field"><label for="name">Naam</label><input id="name" name="source_name" required></div></div>
<button>Bron opslaan</button></form>
<div class="scroll"><table><tr><th>Soort</th><th>Naam</th></tr>{sources}</table></div></details>'''.format(
        csrf=esc(csrf), path=path, commodity=commodity,
        contracts=contract_rows, sources=source_rows)
    if commodity == "electricity":
        choices = '<option value="">Kies contract</option>' + ''.join(
            '<option value="{}">{} · vanaf {}</option>'.format(
                r["id"], esc(r["supplier"]), esc(r["valid_from"])) for r in contracts)
        periods = logic.rows(connection, """
            SELECT p.starts_on, p.ends_on, p.electricity_policy, c.supplier
            FROM settlement_period p JOIN contract c ON c.id=p.contract_id
            WHERE c.commodity='electricity' ORDER BY p.starts_on DESC
        """)
        period_rows = ''.join('<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>'.format(
            esc(r["supplier"]), esc(r["starts_on"]), esc(r["ends_on"]),
            esc("Salderen" if r["electricity_policy"] == "saldering" else
                "Afname en teruglevering apart")) for r in periods)
        result += '''<details><summary>Afrekenwijze stroom instellen</summary>
<p class="hint">Leg vast tot wanneer salderen geldt. De einddatum is de eerste dag na de periode.</p>
<form method="post" action="/settlement"><input type="hidden" name="csrf" value="{csrf}">
<input type="hidden" name="return_to" value="{path}"><div class="grid">
<div class="field"><label for="settlement-contract">Contract</label><select id="settlement-contract" name="contract_id" required>{choices}</select></div>
<div class="field"><label for="settlement-start">Vanaf</label><input id="settlement-start" type="date" name="starts_on" required></div>
<div class="field"><label for="settlement-end">Eerste dag na deze periode</label><input id="settlement-end" type="date" name="ends_on" required></div>
<div class="field"><label for="policy">Wijze</label><select id="policy" name="electricity_policy">
<option value="saldering">Salderen</option><option value="separate_import_export">Afname en teruglevering apart</option></select></div></div>
<button>Periode opslaan</button></form>
<div class="scroll"><table><tr><th>Leverancier</th><th>Vanaf</th><th>Tot</th><th>Wijze</th></tr>{periods}</table></div>
</details>'''.format(csrf=esc(csrf), path=path, choices=choices, periods=period_rows)
    return result
