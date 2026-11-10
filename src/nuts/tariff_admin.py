"""Local tariff entry page. No public listener is published by Compose."""

import base64
import binascii
import hmac
import html
import os
import secrets
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit
from zoneinfo import ZoneInfo

from . import advance_admin, advance_logic, nota, tariff_logic as logic, tariff_overview, tariff_quick


TOKEN = secrets.token_urlsafe(32)
LABELS = {
    "electricity": "Stroom", "gas": "Gas", "water": "Water",
    "supplier": "Leverancier", "network_operator": "Netbeheerder",
    "government": "Overheid", "supply": "Variabele levering",
    "energy_tax": "Belasting op verbruik", "fixed_supply": "Vaste levering",
    "tax_credit": "Vermindering energiebelasting",
    "export_compensation": "Terugleververgoeding",
    "feedin_cost": "Terugleverkosten", "network": "Netbeheerkosten",
    "per_kwh": "per kWh", "per_m3": "per m³", "per_day": "per dag",
    "per_month": "per maand", "per_year": "per jaar",
    "low": "Dal", "high": "Normaal", "all": "Beide / algemeen",
    "import": "Afname", "export": "Teruglevering", "none": "Vast bedrag",
    "saldering": "Salderen", "separate_import_export": "Afname en teruglevering apart",
}


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def component_label(component, commodity):
    if commodity == "water":
        return {"supply": "Drinkwaterverbruik",
                "fixed_supply": "Vastrecht drinkwater",
                "energy_tax": "Belasting op leidingwater"}.get(component, LABELS[component])
    if component == "energy_tax":
        return "Energiebelasting"
    return LABELS[component]


def options(values):
    return "".join('<option value="{}">{}</option>'.format(
        esc(value), esc(LABELS.get(value, value))) for value in values)


def csrf_field():
    return '<input type="hidden" name="csrf" value="{}">'.format(esc(TOKEN))


def database():
    import mysql.connector
    return mysql.connector.connect(
        host=os.environ["DB_HOST"], port=int(os.getenv("DB_PORT", "3306")),
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
        database="nuts_v2", connection_timeout=5,
    )


def export_excess_form(form):
    """Use fixed semantics for compensation above use during saldering."""
    if not form.get("valid_until", "").strip():
        raise logic.InputError("Vul de eerste dag na dit tarief in")
    return {
        "source_id": form.get("source_id", ""),
        "code": "teruglevering_boven_verbruik",
        "component": "export_compensation",
        "flow": "export",
        "register_code": "all",
        "basis": "per_kwh",
        "amount_eur": form.get("amount_eur", ""),
        "includes_vat": "0",
        "vat_rate": "0",
        "valid_from": form.get("valid_from", ""),
        "valid_until": form.get("valid_until", ""),
        "band_quantity": "",
        "band_from": "",
        "band_until": "",
        "notes": "Vergoeding voor netto overschot na salderen",
    }


def fixed_feedin_form(form):
    """A monthly feed-in charge is a fixed cost, optionally in an export band."""
    lower = logic.pick(form, "band_from")
    upper = logic.pick(form, "band_until")
    if upper and not lower:
        raise logic.InputError("Vul ook de ondergrens van de terugleverband in")
    if lower:
        start = logic.amount(lower, "Band vanaf")
        end = logic.amount(upper, "Band tot", optional=True)
        label = "vanaf_{}".format(str(start.normalize()).replace(".", "_"))
        if end is not None:
            label += "_tot_{}".format(str(end.normalize()).replace(".", "_"))
        code = "terugleverkosten_" + label
    else:
        code = "terugleverkosten_vast"
    return {
        "source_id": form.get("source_id", ""),
        "code": code,
        "component": "feedin_cost",
        "flow": "none",
        "register_code": "all",
        "basis": "per_month",
        "amount_eur": form.get("amount_eur", ""),
        "includes_vat": "1",
        "vat_rate": "0.21",
        "valid_from": form.get("valid_from", ""),
        "valid_until": form.get("valid_until", ""),
        "band_quantity": "settlement_export_kwh" if lower else "",
        "band_from": lower,
        "band_until": upper,
        "notes": "Vaste maandelijkse terugleverkosten",
    }


def page(connection, message=""):
    contracts = logic.rows(connection, """
        SELECT id, commodity, supplier, valid_from, valid_until FROM contract
        ORDER BY commodity, valid_from DESC
    """)
    settlements = logic.rows(connection, """
        SELECT p.starts_on, p.ends_on, p.electricity_policy,
               c.supplier, c.valid_from AS contract_from
        FROM settlement_period p JOIN contract c ON c.id=p.contract_id
        ORDER BY p.starts_on DESC
    """)
    sources = logic.rows(connection, """
        SELECT s.id, s.commodity, s.source_kind, s.source_name,
               c.valid_from AS contract_from, c.valid_until AS contract_until
        FROM tariff_source s LEFT JOIN contract c ON c.id=s.contract_id
        ORDER BY s.commodity, s.source_kind, s.id DESC
    """)
    rules = logic.rows(connection, """
        SELECT r.id, r.code, r.component, r.flow, r.register_code,
               r.basis, r.amount_eur, r.includes_vat, r.vat_rate,
               r.valid_from, r.valid_until, r.status, r.band_quantity,
               r.band_from, r.band_until, r.notes, s.commodity, s.source_kind,
               s.source_name
        FROM tariff_rule r JOIN tariff_source s ON s.id=r.tariff_source_id
        ORDER BY r.valid_from DESC, r.id DESC LIMIT 200
    """)
    today = datetime.now(ZoneInfo("Europe/Amsterdam")).date()
    active = logic.rows(connection, """
        SELECT r.component, r.register_code, r.basis, r.amount_eur,
               r.includes_vat, r.vat_rate, r.band_quantity, s.commodity
        FROM tariff_rule r JOIN tariff_source s ON s.id=r.tariff_source_id
        LEFT JOIN contract c ON c.id=s.contract_id
        WHERE r.status='active' AND r.valid_from<=%s
          AND (r.valid_until IS NULL OR r.valid_until>%s)
          AND (s.source_kind<>'supplier' OR
               (c.valid_from<=%s AND (c.valid_until IS NULL OR c.valid_until>%s)))
    """, (today, today, today, today))
    summary, conditional = logic.current_rate_summary(active, today)
    summary_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>€{:.5f}/dag</td><td>€{:.5f}/dag</td><td>€{:.5f}/dag</td><td>€{:.5f}/dag</td></tr>".format(
            esc(LABELS[commodity]),
            ("€{:.5f} dal · €{:.5f} normaal per kWh".format(values["low"], values["high"])
             if commodity == "electricity" else
             "€{:.5f} per m³".format(values["variable_m3"])),
            values["fixed_supply_day"], values["tax_credit_day"],
            values["network_day"], values["feedin_cost_day"])
        for commodity, values in summary.items())
    contract_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            esc(LABELS[row["commodity"]]), esc(row["supplier"]),
            esc(row["valid_from"]), esc(row["valid_until"] or "volgend contract"))
        for row in contracts)
    source_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            esc(LABELS[row["commodity"]]), esc(LABELS[row["source_kind"]]),
            esc(row["source_name"])) for row in sources)
    source_options = '<option value="">Kies bron</option>' + "".join(
        '<option value="{}">{} · {} · {}</option>'.format(
            row["id"], esc(LABELS[row["commodity"]]),
            esc(LABELS[row["source_kind"]]), esc(row["source_name"]))
        for row in sources)
    electricity_suppliers = '<option value="">Kies stroomleverancier</option>' + "".join(
        '<option value="{}">{}</option>'.format(row["id"], esc(row["source_name"]))
        for row in sources if row["commodity"] == "electricity" and
        row["source_kind"] == "supplier")
    settlement_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            esc(row["supplier"]), esc(row["starts_on"]),
            esc(row["ends_on"]), esc(LABELS[row["electricity_policy"]]))
        for row in settlements)
    electricity_contracts = '<option value="">Kies stroomcontract</option>' + "".join(
        '<option value="{}">{} · vanaf {}</option>'.format(
            row["id"], esc(row["supplier"]), esc(row["valid_from"]))
        for row in contracts if row["commodity"] == "electricity")
    rule_rows = []
    for row in rules:
        daily = logic.daily_equivalent(row["amount_eur"], row["basis"], today)
        daily_text = "" if daily is None else " (≈ €{:.5f}/dag)".format(daily)
        vat_text = ("incl. btw" if row["includes_vat"] else
                    "excl. btw; {}%".format(
                        format((row["vat_rate"] * 100).normalize(), "f")))
        band = ""
        if row["band_quantity"]:
            band = " · {} vanaf {}{}".format(
                esc(row["band_quantity"]), esc(row["band_from"]),
                " tot {}".format(esc(row["band_until"])) if row["band_until"] else "")
        action = ""
        if row["status"] == "draft":
            action = ('<form method="post" action="/activate">{}'
                      '<input type="hidden" name="id" value="{}">'
                      '<button>Activeren</button></form>'
                      '<form method="post" action="/discard">{}'
                      '<input type="hidden" name="id" value="{}">'
                      '<button class="muted">Concept verwerpen</button></form>').format(
                          csrf_field(), row["id"], csrf_field(), row["id"])
        elif row["status"] == "active":
            action = '<a href="/withdraw?id={}">Intrekken</a>'.format(row["id"])
        status_label = ("verworpen concept" if row["status"] == "superseded"
                        and (row["notes"] or "").startswith("[verworpen]") else
                        "ingetrokken" if row["status"] == "superseded"
                        and (row["notes"] or "").startswith("[ingetrokken]") else
                        row["status"])
        row_style = ("inactive" if status_label in ("verworpen concept", "ingetrokken")
                     else "tariff-active" if row["status"] == "active"
                     else "tariff-draft" if row["status"] == "draft" else "")
        row_class = ' class="{}"'.format(row_style) if row_style else ""
        rule_rows.append(
            "<tr{}><td>{} · {} · {}</td><td>{}<small>{} · {} · {}{}</small></td>"
            "<td>€{} {}{}<small>{}</small></td><td>{} – {}</td><td>{}</td><td>{}</td></tr>".format(
                row_class,
                esc(LABELS[row["commodity"]]), esc(LABELS[row["source_kind"]]),
                esc(row["source_name"]), esc(component_label(row["component"], row["commodity"])),
                esc(row["code"]), esc(LABELS[row["flow"]]),
                esc(LABELS[row["register_code"]]), band,
                esc(row["amount_eur"]), esc(LABELS[row["basis"]]), daily_text,
                esc(vat_text),
                esc(row["valid_from"]), esc(row["valid_until"] or "volgende invoer"),
                esc(status_label), action))
    return """<!doctype html><html lang="nl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tarievenbeheer</title><style>
body{{font:16px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#183242}}
h1,h2{{color:#125265}}section{{border:1px solid #c6d5dd;border-radius:12px;padding:1rem;margin:1rem 0}}
form{{position:relative;isolation:isolate}}.field{{min-width:0;margin:.7rem 0}}
label{{display:block;margin-bottom:.35rem;font-weight:600}}
input,select{{display:block;width:100%;min-height:2.8rem;padding:.55rem;box-sizing:border-box;
 position:relative;z-index:1;pointer-events:auto;font:inherit;background:#fff;color:#183242;
 border:1px solid #8296a0;border-radius:6px}}
input:focus,select:focus{{outline:3px solid #7bc9c4;outline-offset:2px}}
button{{background:#08736f;color:white;border:0;border-radius:6px;padding:.6rem 1rem;cursor:pointer}}
.muted{{background:#687b83}}td form{{display:inline-block;margin:0 .35rem .35rem 0}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #d9e1e5;padding:.6rem;text-align:left;vertical-align:top}}
.tariff-active td{{background:#edf7ef}}
.tariff-draft td{{background:#fff7da}}
.inactive td{{background:#ffe1e3}}
.scroll{{overflow-x:auto}}small{{display:block;color:#586c78}}.notice{{background:#e7f4ec;padding:.8rem;border-radius:6px}}
.hint{{color:#425863}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:0 1rem}}
.example{{background:#f0f6f7;border-left:4px solid #08736f;padding:.7rem 1rem;margin:.7rem 0}}
</style><main><nav><a href="/">← Stroom, gas en water</a> · <a href="/new">Bedrag invoeren →</a> · <a href="/advances">Voorschotten</a></nav><h1>Uitgebreid tarievenbeheer</h1>
<p class="hint">Stroom, gas en water hebben elk hun eigen contractlijn. Netbeheer en overheid zijn
aparte bronnen. Bedragen blijven in de opgegeven eenheid bewaard.</p>{message}
<section><h2>Actieve tarieven op {today}</h2>
<p class="hint">Variabele bedragen zijn levering plus belasting, inclusief btw. Vaste bedragen zijn
alleen voor deze kalenderdag omgerekend. Netbeheer en terugleverkosten staan apart.
Regels met een verbruiksband of afrekening staan niet in deze eenvoudige optelling
({conditional} zulke regels).</p>
<div class="scroll"><table><tr><th>Soort</th><th>Variabel</th><th>Vaste levering</th><th>Belastingkorting</th><th>Netbeheer</th><th>Terugleverkosten</th></tr>{summary}</table></div></section>
<section><h2>1. Leveringscontract</h2>
<p class="hint">Maak één contract voor stroom, één voor gas en eventueel één voor water.
Ook wanneer stroom en gas dezelfde leverancier hebben, zijn dit twee contractregels.
De leverancier verschijnt daarna vanzelf als tariefbron bij stap 4.</p>
<p class="example">Voorbeeld: kies <strong>Stroom</strong>, vul je leverancier en de ingangsdatum
van het stroomcontract in. Herhaal dit apart voor gas.</p>
<form method="post" action="/contract">{csrf}<div class="grid">
<div class="field"><label for="contract-commodity">Nutssoort</label><select id="contract-commodity" name="commodity">{commodities}</select></div>
<div class="field"><label for="contract-supplier">Leverancier</label><input id="contract-supplier" name="supplier" maxlength="120" required></div>
<div class="field"><label for="contract-start">Ingangsdatum contract</label><input id="contract-start" name="valid_from" type="date" required></div>
<div class="field"><label for="contract-description">Omschrijving (optioneel)</label><input id="contract-description" name="description" maxlength="200"></div></div>
<button>Contract opslaan</button></form><div class="scroll"><table><tr><th>Soort</th><th>Leverancier</th><th>Vanaf</th><th>Tot</th></tr>{contracts}</table></div></section>
<section><h2>2. Afrekenwijze stroom</h2>
<p class="hint">Alleen voor stroom. Kies het contract uit stap 1 en leg vast hoe
teruglevering in die periode wordt verrekend. Vul het begin en de eerste dag
<em>na</em> de periode in. Een volgende periode mag op die einddatum starten.
Dit onderdeel kun je later invullen als je eerst de gewone tarieven wilt vastleggen.</p>
<form method="post" action="/settlement">{csrf}<div class="grid">
<div class="field"><label for="settlement-contract">Stroomcontract</label><select id="settlement-contract" name="contract_id" required>{electricity_contracts}</select></div>
<div class="field"><label for="settlement-start">Begindatum</label><input id="settlement-start" name="starts_on" type="date" required></div>
<div class="field"><label for="settlement-end">Eerste dag na deze periode</label><input id="settlement-end" name="ends_on" type="date" required></div>
<div class="field"><label for="settlement-policy">Afrekenwijze</label><select id="settlement-policy" name="electricity_policy">{policies}</select></div></div>
<button>Periode opslaan</button></form><div class="scroll"><table><tr><th>Leverancier</th><th>Vanaf</th><th>Tot</th><th>Wijze</th></tr>{settlements}</table></div></section>
<section><h2>3. Netbeheerder of overheid</h2>
<p class="hint">Voeg hier alleen bronnen toe die niet uit een leveringscontract komen.
De leverancier uit stap 1 staat al klaar. Maak zo nodig een aparte bron per nutssoort.</p>
<p class="example">Voorbeeld: <strong>Stroom · Netbeheerder · Liander</strong>.
Voor energiebelasting: <strong>Stroom · Overheid · Energiebelasting</strong>.
Herhaal voor gas als die tarieven anders zijn.</p>
<form method="post" action="/source">{csrf}<div class="grid">
<div class="field"><label for="source-commodity">Nutssoort</label><select id="source-commodity" name="commodity">{commodities}</select></div>
<div class="field"><label for="source-kind">Wie berekent dit?</label><select id="source-kind" name="source_kind">{source_kinds}</select></div>
<div class="field"><label for="source-name">Naam van de bron</label><input id="source-name" name="source_name" maxlength="120" required></div></div>
<button>Bron opslaan</button></form><div class="scroll"><table><tr><th>Soort</th><th>Bron</th><th>Naam</th></tr>{sources}</table></div></section>
<details><summary>Uitgebreide tariefinvoer voor uitzonderingen</summary>
<section><h2>4. Bedragen uit je tariefkaart</h2>
<p class="hint">Maak voor <strong>ieder bedrag</strong> een eigen tariefregel. Kies eerst wie het
bedrag rekent: leverancier uit stap 1, netbeheerder of overheid uit stap 3.
Gebruik bij een nieuw bedrag volgend jaar dezelfde naam en de nieuwe ingangsdatum;
bij activering sluit de oude regel dan vanzelf.</p>
<div class="scroll"><table><tr><th>Wat staat op de tariefkaart?</th><th>Bron en onderdeel</th><th>Richting · telwerk · eenheid</th><th>Bedrag</th></tr>
<tr><td>Stroom levering normaal</td><td>Stroomleverancier · Variabele levering</td><td>Afname · Normaal · per kWh</td><td>€0,15010</td></tr>
<tr><td>Stroom levering dal</td><td>Stroomleverancier · Variabele levering</td><td>Afname · Dal · per kWh</td><td>€0,14768</td></tr>
<tr><td>Stroom energiebelasting</td><td>Overheid stroom · Energiebelasting</td><td>Afname · Beide · per kWh</td><td>€0,11085</td></tr>
<tr><td>Gas levering</td><td>Gasleverancier · Variabele levering</td><td>Afname · Beide · per m³</td><td>€0,71510</td></tr>
<tr><td>Vaste levering gas</td><td>Gasleverancier · Vaste levering</td><td>Vast bedrag · Beide · per maand</td><td>€5,99</td></tr>
<tr><td>Netbeheer gas</td><td>Netbeheerder gas · Netbeheerkosten</td><td>Vast bedrag · Beide · per jaar</td><td>€266,63</td></tr>
<tr><td>Vaste terugleverkosten</td><td>Stroomleverancier · Terugleverkosten</td><td>Vast bedrag · Beide · per maand</td><td>€9,47 bij de voorbeeldband</td></tr>
<tr><td>Vermindering energiebelasting</td><td>Overheid stroom · Vermindering energiebelasting</td><td>Vast bedrag · Beide · per jaar</td><td>−€628,96</td></tr></table></div>
<p class="hint">Deze voorbeeldbedragen komen uit je aangeleverde kaart. Gebruik de datums
en bedragen van je eigen contract. Het totaal van €0,26095/kWh is al levering plus
energiebelasting; voer dat totaal niet nog een keer in. De voorbeeldkaart is inclusief btw.</p>
<h3>Teruglevering boven verbruik (geen btw)</h3>
<p class="hint">Gebruik dit korte formulier voor de vergoeding op het overschot na
salderen. De keuzes <strong>Terugleververgoeding · Teruglevering · Beide · per kWh · 0% btw</strong>
staan al vast. Dit bedrag wordt pas bij de afrekening op het netto overschot toegepast,
dus niet als gewone dagopbrengst geboekt.</p>
<form method="post" action="/export-excess">{csrf}<div class="grid">
<div class="field"><label for="export-source">Stroomleverancier</label><select id="export-source" name="source_id" required>{electricity_suppliers}</select></div>
<div class="field"><label for="export-amount">Vergoeding per kWh, met minteken</label><input id="export-amount" name="amount_eur" inputmode="decimal" placeholder="-0,15000" required></div>
<div class="field"><label for="export-start">Ingangsdatum</label><input id="export-start" name="valid_from" type="date" required></div>
<div class="field"><label for="export-end">Eerste dag waarop dit tarief niet meer geldt</label><input id="export-end" name="valid_until" type="date" required></div></div>
<button>Terugleververgoeding als concept opslaan</button></form>
<h3>Vaste terugleverkosten per maand</h3>
<p class="hint">Dit is een bedrag dat je <strong>betaalt</strong> voor teruglevering.
Het is geen vergoeding en geen prijs per kWh. De keuzes
<strong>Terugleverkosten · Vast bedrag · Beide · per maand</strong> staan vast.
Gebruik een positieve prijs. Als het bedrag afhangt van de jaarhoeveelheid
teruglevering, vul ook de band in; die wordt pas bij de afrekening bepaald.</p>
<form method="post" action="/feedin-fixed">{csrf}<div class="grid">
<div class="field"><label for="feedin-source">Stroomleverancier</label><select id="feedin-source" name="source_id" required>{electricity_suppliers}</select></div>
<div class="field"><label for="feedin-amount">Bedrag per maand, inclusief btw</label><input id="feedin-amount" name="amount_eur" inputmode="decimal" placeholder="9,47" required></div>
<div class="field"><label for="feedin-start">Ingangsdatum</label><input id="feedin-start" name="valid_from" type="date" required></div>
<div class="field"><label for="feedin-end">Eerste dag na dit tarief (optioneel)</label><input id="feedin-end" name="valid_until" type="date"></div>
<div class="field"><label for="feedin-from">Jaarlijkse teruglevering vanaf kWh (optioneel)</label><input id="feedin-from" name="band_from" inputmode="decimal" placeholder="751"></div>
<div class="field"><label for="feedin-until">Jaarlijkse teruglevering tot kWh, exclusief (optioneel)</label><input id="feedin-until" name="band_until" inputmode="decimal" placeholder="1001"><small>Voor 751 t/m 1000 kWh vul je 751 en 1001 in.</small></div></div>
<button>Vaste terugleverkosten als concept opslaan</button></form>
<h3>Overige tariefregels</h3>
<form method="post" action="/rule">{csrf}<div class="grid">
<div class="field"><label for="rule-source">Wie brengt dit bedrag in rekening?</label><select id="rule-source" name="source_id" required>{source_options}</select></div>
<div class="field"><label for="rule-code">Naam van deze tariefregel</label><input id="rule-code" name="code" placeholder="levering stroom normaal" required><small>Gebruik dezelfde naam bij een nieuw tarief voor dit onderdeel.</small></div>
<div class="field"><label for="rule-component">Welk onderdeel?</label><select id="rule-component" name="component">{components}</select></div>
<div class="field"><label for="rule-flow">Afname, teruglevering of vast bedrag?</label><select id="rule-flow" name="flow">{flows}</select></div>
<div class="field"><label for="rule-register">Welk stroomtelwerk?</label><select id="rule-register" name="register_code">{registers}</select><small>Bij gas, water en vaste bedragen: Beide / algemeen.</small></div>
<div class="field"><label for="rule-amount">Bedrag in euro</label><input id="rule-amount" name="amount_eur" inputmode="decimal" placeholder="0,15010" required><small>Een vergoeding of belastingkorting voer je negatief in.</small></div>
<div class="field"><label for="rule-basis">Waarvoor geldt dat bedrag?</label><select id="rule-basis" name="basis">{bases}</select></div>
<div class="field"><label for="rule-vat">Is het ingevulde bedrag inclusief btw?</label><select id="rule-vat" name="includes_vat"><option value="1">Ja, inclusief btw</option><option value="0">Nee, exclusief btw</option></select></div>
<div class="field"><label for="rule-vat-rate">Btw-percentage</label><input id="rule-vat-rate" name="vat_rate" value="21" inputmode="decimal"><small>Bijvoorbeeld 21. Bij een bedrag zonder btw: 0.</small></div>
<div class="field"><label for="rule-start">Ingangsdatum van dit bedrag</label><input id="rule-start" name="valid_from" type="date" required></div>
<div class="field"><label for="rule-end">Einddatum (meestal leeg)</label><input id="rule-end" name="valid_until" type="date"></div></div>
<details><summary>Alleen voor verbruiksbanden of extra toelichting</summary>
<p class="hint">Laat dit leeg voor gewone tarieven. Bij een tarief voor bijvoorbeeld
751–1000 kWh teruglevering kies je de terugleverband en vul je de grenzen in.</p>
<div class="grid"><div class="field"><label for="rule-band">Welke verbruiksband?</label><select id="rule-band" name="band_quantity">{bands}</select></div>
<div class="field"><label for="rule-band-from">Band vanaf</label><input id="rule-band-from" name="band_from" inputmode="decimal"></div>
<div class="field"><label for="rule-band-until">Band tot</label><input id="rule-band-until" name="band_until" inputmode="decimal"></div>
<div class="field"><label for="rule-notes">Toelichting</label><input id="rule-notes" name="notes" maxlength="255"></div></div></details>
<button>Tarief als concept opslaan</button></form></section></details>
<section><h2>Opgeslagen tariefregels</h2><p class="hint">Controleer bron, bedrag, btw, richting, telwerk, eenheid en datum voordat je activeert.
Een concept kun je verwerpen; het telt dan niet mee. Een verkeerd ingevoerd actief tarief
kun je intrekken en opnieuw invoeren. Ingetrokken regels blijven zichtbaar voor de historie.</p>
<div class="scroll"><table><tr><th>Bron</th><th>Onderdeel</th><th>Bedrag</th><th>Periode</th><th>Status</th><th></th></tr>{rules}</table></div></section>
</main></html>""".format(
        message='<p class="notice">{}</p>'.format(esc(message)) if message else "",
        csrf=csrf_field(), commodities=options(logic.COMMODITIES),
        today=esc(today), summary=summary_rows, conditional=len(conditional),
        source_kinds=options(logic.SOURCE_KINDS), contracts=contract_rows,
        electricity_contracts=electricity_contracts,
        policies=options(logic.POLICIES), settlements=settlement_rows,
        sources=source_rows, source_options=source_options,
        electricity_suppliers=electricity_suppliers,
        components=options(logic.COMPONENTS), flows=options(logic.FLOWS),
        registers=options(logic.REGISTERS), bases=options(logic.BASES),
        bands='<option value="">Geen</option>' + options(logic.BANDS[1:]),
        rules="".join(rule_rows))


def withdrawal_page(connection, rule_id):
    rule = logic.one(connection, """
        SELECT r.status, r.amount_eur, r.basis, r.valid_from, r.valid_until,
               r.code, s.source_name, s.commodity
        FROM tariff_rule r JOIN tariff_source s ON s.id=r.tariff_source_id
        WHERE r.id=%s
    """, (rule_id,))
    if rule is None or rule["status"] != "active":
        raise logic.InputError("Dit tarief is niet meer actief")
    back = tariff_overview.PATHS[rule["commodity"]]
    return '''<!doctype html><html lang="nl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tarief intrekken</title>
<style>body{{font:16px system-ui,sans-serif;max-width:650px;margin:2rem auto;padding:0 1rem;color:#183242}}
button{{background:#9f352b;color:white;border:0;border-radius:6px;padding:.7rem 1rem;cursor:pointer}}
.card{{border:1px solid #c6d5dd;border-radius:12px;padding:1rem;margin:1rem 0}}</style>
<main><p><a href="{back}">← Tarievenoverzicht</a></p><h1>Tarief intrekken</h1>
<div class="card"><strong>{source}</strong><p>{code}: €{amount} {basis}<br>
Vanaf {start} tot {end}</p></div>
<p>Dit tarief telt daarna niet meer mee. De regel blijft zichtbaar in de historie.
Voer het juiste tarief daarna opnieuw in.</p>
<form method="post" action="/withdraw">{csrf}
<input type="hidden" name="id" value="{id}">
<input type="hidden" name="return_to" value="{back}">
<input type="hidden" name="confirm" value="1"><button>Ja, dit tarief intrekken</button></form>
</main></html>'''.format(
        source=esc(rule["source_name"]), code=esc(rule["code"]),
        amount=esc(rule["amount_eur"]), basis=esc(LABELS[rule["basis"]]),
        start=esc(rule["valid_from"]), end=esc(rule["valid_until"] or "volgende invoer"),
        csrf=csrf_field(), id=rule_id, back=back)


def return_page(form):
    target = form.get("return_to", "")
    return target if target in tariff_overview.PATHS.values() else "/setup"


class Handler(BaseHTTPRequestHandler):
    def authorized(self):
        header = self.headers.get("Authorization", "")
        try:
            if not header.startswith("Basic "):
                return False
            username, password = base64.b64decode(
                header[6:], validate=True).decode("utf-8").split(":", 1)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return False
        return (hmac.compare_digest(username, os.environ["TARIFF_ADMIN_USER"])
                and hmac.compare_digest(password, os.environ["TARIFF_ADMIN_PASSWORD"]))

    def require_auth(self):
        if self.authorized():
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Tarievenbeheer", charset="UTF-8"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return False

    def html(self, status, body):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.require_auth():
            return
        path = urlsplit(self.path).path
        if path not in ("/", "/stroom", "/gas", "/water", "/archief",
                        "/setup", "/advances", "/nota", "/new", "/withdraw"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        connection = None
        started = time.monotonic()
        connected = started
        try:
            query = parse_qs(urlsplit(self.path).query)
            notice = query.get("notice", [""])[0][:200]
            selected = query.get("type", [""])[0]
            commodity = query.get("commodity", [""])[0]
            if path == "/archief":
                commodity = query.get("soort", [""])[0]
                if commodity not in tariff_overview.PATHS:
                    raise logic.InputError("Kies stroom, gas of water voor het archief")
            if path in ("/stroom", "/gas", "/water"):
                commodity = {value: key for key, value in tariff_overview.PATHS.items()}[path]
            if path not in ("/", "/new") or selected in tariff_quick.TEMPLATES:
                connection = database()
                connected = time.monotonic()
            if path == "/":
                rendered = tariff_overview.home_page()
            elif path in ("/stroom", "/gas", "/water", "/archief"):
                rendered = tariff_overview.commodity_page(
                    connection, commodity, TOKEN, path == "/archief", notice)
            elif path == "/setup":
                rendered = page(connection, notice)
            elif path == "/advances":
                rendered = advance_admin.page(connection, TOKEN, notice)
            elif path == "/nota":
                rendered = nota.page(connection)
            elif path == "/withdraw":
                rendered = withdrawal_page(
                    connection, logic.positive_id(query.get("id", [""])[0], "Tariefregel"))
            else:
                rendered = tariff_quick.page(connection, TOKEN, selected, notice, commodity)
            self.html(HTTPStatus.OK, rendered)
        except logic.InputError as exc:
            self.html(HTTPStatus.BAD_REQUEST,
                      "<h1>Controleer invoer</h1><p>{}</p><p><a href='/'>Terug</a></p>".format(esc(exc)))
        except Exception as exc:
            print("Tarievenpagina: {}".format(exc), flush=True)
            self.html(HTTPStatus.SERVICE_UNAVAILABLE, "Database tijdelijk niet bereikbaar")
        finally:
            if connection is not None:
                connection.close()
            elapsed = time.monotonic() - started
            if elapsed >= 0.5:
                print("Langzame pagina {}: verbinden {:.2f}s, laden/verzenden {:.2f}s".format(
                    path, connected - started, elapsed - (connected - started)), flush=True)

    def do_POST(self):
        if not self.require_auth():
            return
        path = urlsplit(self.path).path
        if path not in ("/contract", "/settlement", "/source", "/rule",
                        "/export-excess", "/feedin-fixed", "/activate", "/discard",
                        "/advance", "/advance-void", "/new", "/withdraw"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        try:
            started = time.monotonic()
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 16384:
                raise logic.InputError("Formulier is te groot of leeg")
            raw = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            form = {key: values[0] for key, values in raw.items()}
            if not hmac.compare_digest(form.get("csrf", ""), TOKEN):
                raise logic.InputError("Formulier verlopen; laad de pagina opnieuw")
            connection = database()
            try:
                if path == "/contract":
                    logic.save_contract(connection, form)
                    message = "Contract opgeslagen; vorige contractdatum afgesloten."
                elif path == "/settlement":
                    logic.save_settlement(connection, form)
                    message = "Afrekenperiode opgeslagen."
                elif path == "/source":
                    logic.save_source(connection, form)
                    message = "Tariefbron opgeslagen."
                elif path == "/rule":
                    logic.save_rule(connection, form, os.environ["TARIFF_ADMIN_USER"])
                    message = "Concept opgeslagen. Controleer en activeer het hieronder."
                elif path == "/export-excess":
                    logic.save_rule(connection, export_excess_form(form),
                                    os.environ["TARIFF_ADMIN_USER"])
                    message = "Terugleververgoeding als concept opgeslagen. Controleer en activeer het hieronder."
                elif path == "/feedin-fixed":
                    logic.save_rule(connection, fixed_feedin_form(form),
                                    os.environ["TARIFF_ADMIN_USER"])
                    message = "Vaste terugleverkosten als concept opgeslagen. Controleer en activeer het hieronder."
                elif path == "/advance":
                    advance_logic.save_schedule(connection, form)
                    message = "Voorschot opgeslagen."
                elif path == "/advance-void":
                    advance_logic.void_schedule(
                        connection, logic.positive_id(form.get("id", ""), "Voorschot"))
                    message = "Voorschot verworpen; het telt niet mee."
                elif path == "/new":
                    label = tariff_quick.save_and_activate(
                        connection, form, os.environ["TARIFF_ADMIN_USER"])
                    message = "{} opgeslagen en geactiveerd.".format(label)
                elif path == "/activate":
                    logic.activate_rule(connection, logic.positive_id(form.get("id", ""), "Tariefregel"))
                    message = "Tariefregel geactiveerd; een vorig tarief voor hetzelfde onderdeel is zo nodig afgesloten."
                elif path == "/withdraw":
                    if form.get("confirm") != "1":
                        raise logic.InputError("Bevestig dat je dit tarief wilt intrekken")
                    logic.withdraw_rule(connection, logic.positive_id(form.get("id", ""), "Tariefregel"))
                    message = "Tarief ingetrokken; voer de juiste regel opnieuw in."
                else:
                    logic.discard_rule(connection, logic.positive_id(form.get("id", ""), "Tariefregel"))
                    message = "Concept verworpen; het telt niet mee in de berekeningen."
                connection.commit()
                self.send_response(HTTPStatus.SEE_OTHER)
                if path.startswith("/advance"):
                    destination = "/advances"
                elif path == "/new":
                    destination = tariff_overview.PATHS[tariff_quick.TEMPLATES[form["template"]][2]]
                else:
                    destination = return_page(form)
                destination += "?notice="
                self.send_header("Location", destination + quote(message))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        except logic.InputError as exc:
            destination = "/advances" if path.startswith("/advance") else "/setup"
            if path == "/new":
                destination = "/new?type=" + quote(form.get("template", "")) if "form" in locals() else "/new"
            elif "form" in locals():
                destination = return_page(form)
            self.html(HTTPStatus.BAD_REQUEST,
                      "<h1>Controleer invoer</h1><p>{}</p><p><a href='{}'>Terug</a></p>".format(
                          esc(exc), destination))
        except Exception as exc:
            print("Tariefopslag: {}".format(exc), flush=True)
            self.html(HTTPStatus.INTERNAL_SERVER_ERROR, "Opslaan mislukt; controleer de containerlog")
        finally:
            if "started" in locals():
                elapsed = time.monotonic() - started
                if elapsed >= 0.5:
                    print("Langzame opslag {}: {:.2f}s".format(path, elapsed), flush=True)


def main():
    if not os.getenv("TARIFF_ADMIN_USER") or not os.getenv("TARIFF_ADMIN_PASSWORD"):
        raise SystemExit("TARIFF_ADMIN_USER en TARIFF_ADMIN_PASSWORD ontbreken")
    port = 8765
    print("Tarievenbeheer op poort {}".format(port), flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
