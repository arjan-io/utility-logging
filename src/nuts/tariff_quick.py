"""Short forms for recognizable lines on a utility bill."""

import html
from urllib.parse import quote

from . import tariff_logic as db


# label, group, commodity, source kind, component, flow, register, basis
TEMPLATES = {
    "stroom_normaal": ("Stroom normaal per kWh", "Levering", "electricity", "supplier", "supply", "import", "high", "per_kwh"),
    "stroom_dal": ("Stroom dal per kWh", "Levering", "electricity", "supplier", "supply", "import", "low", "per_kwh"),
    "stroom_vast": ("Vaste levering stroom", "Levering", "electricity", "supplier", "fixed_supply", "none", "all", "fixed"),
    "gas_m3": ("Gas per m³", "Levering", "gas", "supplier", "supply", "import", "all", "per_m3"),
    "gas_vast": ("Vaste levering gas", "Levering", "gas", "supplier", "fixed_supply", "none", "all", "fixed"),
    "water_m3": ("Water per m³", "Levering", "water", "supplier", "supply", "import", "all", "per_m3"),
    "water_vast": ("Vaste levering water", "Levering", "water", "supplier", "fixed_supply", "none", "all", "fixed"),
    "stroom_belasting": ("Energiebelasting stroom", "Belasting", "electricity", "government", "energy_tax", "import", "all", "per_kwh"),
    "gas_belasting": ("Energiebelasting gas", "Belasting", "gas", "government", "energy_tax", "import", "all", "per_m3"),
    "belastingkorting": ("Vermindering energiebelasting", "Belasting", "electricity", "government", "tax_credit", "none", "all", "fixed"),
    "netbeheer_stroom": ("Netbeheer stroom", "Netbeheer", "electricity", "network_operator", "network", "none", "all", "fixed"),
    "netbeheer_gas": ("Netbeheer gas", "Netbeheer", "gas", "network_operator", "network", "none", "all", "fixed"),
    "netbeheer_water": ("Netbeheer water", "Netbeheer", "water", "network_operator", "network", "none", "all", "fixed"),
    "terugleververgoeding": ("Terugleververgoeding boven verbruik (salderen)", "Zonnepanelen", "electricity", "supplier", "export_compensation", "export", "all", "per_kwh"),
    "terugleververgoeding_apart": ("Terugleververgoeding zonder salderen", "Zonnepanelen", "electricity", "supplier", "export_compensation", "export", "all", "per_kwh"),
    "terugleverkosten": ("Vaste terugleverkosten per maand", "Zonnepanelen", "electricity", "supplier", "feedin_cost", "none", "all", "per_month"),
    "terugleverkosten_kwh": ("Terugleverkosten per teruggeleverde kWh", "Zonnepanelen", "electricity", "supplier", "feedin_cost", "export", "all", "per_kwh"),
}

COMMODITY = {"electricity": "stroom", "gas": "gas", "water": "water"}
SOURCE = {"supplier": "leverancier", "government": "overheid", "network_operator": "netbeheerder"}


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def prepared(form):
    key = db.pick(form, "template")
    if key not in TEMPLATES:
        raise db.InputError("Kies eerst het bedrag op je tariefkaart")
    label, _, commodity, source_kind, component, flow, register, basis = TEMPLATES[key]
    basis = db.pick(form, "basis") if basis == "fixed" else basis
    lower = db.pick(form, "band_from") if key == "terugleverkosten" else ""
    upper = db.pick(form, "band_until") if key == "terugleverkosten" else ""
    if upper and not lower:
        raise db.InputError("Vul ook de ondergrens van de terugleverband in")
    code = "snel_" + key
    if lower:
        low = db.amount(lower, "Band vanaf")
        high = db.amount(upper, "Band tot", optional=True)
        code += "_" + str(low.normalize()).replace(".", "_")
        if high is not None:
            code += "_" + str(high.normalize()).replace(".", "_")
    if key == "terugleververgoeding" and not db.pick(form, "valid_until"):
        raise db.InputError("Vul de eerste dag na deze terugleververgoeding in")
    rule = dict(source_id=db.pick(form, "source_id"), code=code,
                component=component, flow=flow, register_code=register,
                basis=basis, amount_eur=db.pick(form, "amount_eur"),
                includes_vat="0" if key.startswith("terugleververgoeding") else db.pick(form, "includes_vat"),
                vat_rate="0" if key.startswith("terugleververgoeding") else db.pick(form, "vat_rate"),
                valid_from=db.pick(form, "valid_from"),
                valid_until=db.pick(form, "valid_until"),
                band_quantity="settlement_export_kwh" if lower else "",
                band_from=lower, band_until=upper,
                notes="Eenvoudige invoer: " + label)
    return key, commodity, source_kind, rule


def save_and_activate(connection, form, actor):
    key, commodity, kind, rule = prepared(form)
    source_id = db.positive_id(rule["source_id"], "Bron")
    source = db.one(connection, """
        SELECT commodity, source_kind FROM tariff_source WHERE id=%s
    """, (source_id,))
    if source is None or source["commodity"] != commodity or source["source_kind"] != kind:
        raise db.InputError("Kies een bron voor {} ({})".format(COMMODITY[commodity], SOURCE[kind]))
    existing = db.rows(connection, """
        SELECT code, component, flow, register_code, band_quantity,
               band_from, band_until FROM tariff_rule
        WHERE tariff_source_id=%s AND status='active'
        ORDER BY valid_from DESC, id DESC
    """, (source_id,))
    lower = db.amount(rule["band_from"], "Band vanaf", optional=True)
    upper = db.amount(rule["band_until"], "Band tot", optional=True)
    matches = {row["code"] for row in existing
               if row["component"] == rule["component"] and
               row["flow"] == rule["flow"] and
               row["register_code"] == rule["register_code"] and
               row["band_quantity"] == (rule["band_quantity"] or None) and
               row["band_from"] == lower and row["band_until"] == upper}
    if len(matches) > 1:
        raise db.InputError("Meerdere bestaande tarieven voor dit bedrag; gebruik uitgebreide invoer")
    if matches:
        rule["code"] = matches.pop()
    rule_id = db.save_rule(connection, rule, actor)
    db.activate_rule(connection, rule_id)
    return TEMPLATES[key][0]


def page(connection, csrf, selected="", message=""):
    notice = '<p class="notice">{}</p>'.format(esc(message)) if message else ""
    if selected not in TEMPLATES:
        content = '<p>Kies het bedrag dat op je tariefkaart staat. De technische keuzes vullen we voor je in.</p>'
        for group in ("Levering", "Belasting", "Netbeheer", "Zonnepanelen"):
            links = "".join('<a class="choice" href="/new?type={}">{}</a>'.format(
                quote(key), esc(row[0])) for key, row in TEMPLATES.items() if row[1] == group)
            content += '<section><h2>{}</h2><div class="choices">{}</div></section>'.format(group, links)
    else:
        label, _, commodity, kind, _, _, _, basis = TEMPLATES[selected]
        sources = db.rows(connection, """
            SELECT s.id, s.source_name, c.valid_from AS contract_from
            FROM tariff_source s LEFT JOIN contract c ON c.id=s.contract_id
            WHERE s.commodity=%s AND s.source_kind=%s ORDER BY s.id DESC
        """, (commodity, kind))
        choices = '<option value="">Kies {}</option>'.format(SOURCE[kind]) + "".join(
            '<option value="{}">{}{}{}</option>'.format(
                row["id"], esc(row["source_name"]),
                " · contract vanaf " if row["contract_from"] else "",
                esc(row["contract_from"])) for row in sources)
        units = ('<select id="basis" name="basis">'
                 '<option value="per_month">per maand</option>'
                 '<option value="per_day">per dag</option>'
                 '<option value="per_year">per jaar</option></select>'
                 if basis == "fixed" else '<strong>{}</strong>'.format(
                     {"per_kwh": "per kWh", "per_m3": "per m³", "per_month": "per maand"}[basis]))
        vat = ('' if selected.startswith("terugleververgoeding") else
               '<div class="field"><label for="incl">Bedrag op kaart</label><select id="incl" name="includes_vat">'
               '<option value="1">inclusief btw</option><option value="0">exclusief btw</option></select></div>'
               '<div class="field"><label for="vat">Btw-percentage</label>'
               '<input id="vat" name="vat_rate" value="21" inputmode="decimal"></div>')
        band = ('' if selected != "terugleverkosten" else
                '<fieldset><legend>Terugleverband, als die op je tariefkaart staat</legend>'
                '<p>Voor 751 t/m 1000 kWh: vanaf 751, tot 1001. Laat beide velden leeg als er geen band is.</p><div class="grid">'
                '<div class="field"><label for="low">Vanaf kWh</label><input id="low" name="band_from"></div>'
                '<div class="field"><label for="high">Tot kWh, exclusief</label><input id="high" name="band_until"></div>'
                '</div></fieldset>')
        no_sources = ('' if sources else
                      '<p class="hint">Er is nog geen passende bron. Maak op het '
                      '<a href="/">tarievenoverzicht</a> eerst een contract en bron aan.</p>')
        end_required = ' required' if selected == "terugleververgoeding" else ''
        sign_hint = ('Gebruik een minteken: dit bedrag ontvang je.' if selected.startswith("terugleververgoeding") else
                     'Gebruik een minteken: dit is een korting.' if selected == "belastingkorting" else
                     'Gebruik een positief bedrag: dit betaal je.')
        content = '''<p><a href="/new">← Ander bedrag kiezen</a></p><section><h2>{label}</h2>
<p class="hint">Bron: {kind} voor {commodity}. {hint}</p>{no_sources}
<form method="post" action="/new"><input type="hidden" name="csrf" value="{csrf}">
<input type="hidden" name="template" value="{selected}"><div class="grid">
<div class="field"><label for="source">Wie rekent dit?</label><select id="source" name="source_id" required>{choices}</select></div>
<div class="field"><label for="amount">Bedrag in euro</label><input id="amount" name="amount_eur" inputmode="decimal" required></div>
<div class="field"><label for="basis">Eenheid</label>{units}</div>
<div class="field"><label for="start">Ingangsdatum</label><input id="start" name="valid_from" type="date" required></div>
<div class="field"><label for="end">Eerste dag na dit tarief (optioneel)</label><input id="end" name="valid_until" type="date"{end_required}></div>
{vat}</div>{band}<button>Opslaan en activeren</button></form></section>'''.format(
            label=esc(label), kind=SOURCE[kind], commodity=COMMODITY[commodity],
            hint=esc(sign_hint), csrf=esc(csrf), selected=esc(selected),
            choices=choices, units=units, end_required=end_required,
            vat=vat, band=band, no_sources=no_sources)
    return '''<!doctype html><html lang="nl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Tarief invoeren</title>
<style>body{{font:16px system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#183242}}
h1,h2{{color:#125265}}section{{border:1px solid #c6d5dd;border-radius:12px;padding:1rem;margin:1rem 0}}
.choices,.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.6rem}}
.choice{{display:block;background:#e7f4ec;padding:.8rem;border-radius:6px;text-decoration:none;color:#125265}}
.field{{min-width:0;margin:.5rem 0}}label{{display:block;margin-bottom:.35rem;font-weight:600}}
input,select{{display:block;width:100%;min-height:2.8rem;padding:.55rem;box-sizing:border-box;font:inherit;
background:white;color:#183242;border:1px solid #8296a0;border-radius:6px}}
input:focus,select:focus,.choice:focus{{outline:3px solid #7bc9c4;outline-offset:2px}}
button{{background:#08736f;color:white;border:0;border-radius:6px;padding:.6rem 1rem;cursor:pointer}}
.hint{{color:#425863}}.notice{{background:#e7f4ec;padding:.8rem;border-radius:6px}}</style>
<main><nav><a href="/">← Tarievenoverzicht</a> · <a href="/advances">Voorschotten</a></nav>
<h1>Bedrag van je tariefkaart invoeren</h1>{notice}{content}</main></html>'''.format(
        notice=notice, content=content)
