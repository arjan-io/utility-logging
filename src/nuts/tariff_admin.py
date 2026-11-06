"""Local tariff entry page. No public listener is published by Compose."""

import base64
import binascii
import hmac
import html
import os
import secrets
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, quote, urlsplit
from zoneinfo import ZoneInfo

from . import tariff_logic as logic


TOKEN = secrets.token_urlsafe(32)
LABELS = {
    "electricity": "Stroom", "gas": "Gas", "water": "Water",
    "supplier": "Leverancier", "network_operator": "Netbeheerder",
    "government": "Overheid", "supply": "Variabele levering",
    "energy_tax": "Energiebelasting", "fixed_supply": "Vaste levering",
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
               r.band_from, r.band_until, s.commodity, s.source_name
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
        "<tr><td>{}</td><td>{}</td><td>€{:.5f}/dag</td><td>€{:.5f}/dag</td><td>€{:.5f}/dag</td></tr>".format(
            esc(LABELS[commodity]),
            ("€{:.5f} dal · €{:.5f} normaal per kWh".format(values["low"], values["high"])
             if commodity == "electricity" else
             "€{:.5f} per m³".format(values["variable_m3"])),
            values["fixed_supply_day"], values["tax_credit_day"],
            values["network_day"])
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
        band = ""
        if row["band_quantity"]:
            band = " · {} vanaf {}{}".format(
                esc(row["band_quantity"]), esc(row["band_from"]),
                " tot {}".format(esc(row["band_until"])) if row["band_until"] else "")
        action = ""
        if row["status"] == "draft":
            action = ('<form method="post" action="/activate">{}'
                      '<input type="hidden" name="id" value="{}">'
                      '<button>Activeren</button></form>').format(csrf_field(), row["id"])
        rule_rows.append(
            "<tr><td>{} · {}</td><td>{}<small>{}{}</small></td>"
            "<td>€{} {}{}</td><td>{} – {}</td><td>{}</td><td>{}</td></tr>".format(
                esc(LABELS[row["commodity"]]), esc(row["source_name"]),
                esc(LABELS[row["component"]]), esc(row["code"]), band,
                esc(row["amount_eur"]), esc(LABELS[row["basis"]]), daily_text,
                esc(row["valid_from"]), esc(row["valid_until"] or "volgende invoer"),
                esc(row["status"]), action))
    return """<!doctype html><html lang="nl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tarievenbeheer</title><style>
body{{font:16px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#183242}}
h1,h2{{color:#125265}}section{{border:1px solid #c6d5dd;border-radius:12px;padding:1rem;margin:1rem 0}}
label{{display:block;margin:.7rem 0}}input,select{{display:block;width:100%;max-width:35rem;padding:.55rem;box-sizing:border-box}}
button{{background:#08736f;color:white;border:0;border-radius:6px;padding:.6rem 1rem;cursor:pointer}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #d9e1e5;padding:.6rem;text-align:left;vertical-align:top}}
.scroll{{overflow-x:auto}}small{{display:block;color:#586c78}}.notice{{background:#e7f4ec;padding:.8rem;border-radius:6px}}
.hint{{color:#425863}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:0 1rem}}
</style><main><h1>Tarievenbeheer</h1>
<p class="hint">Stroom, gas en water hebben elk hun eigen contractlijn. Netbeheer en overheid zijn
aparte bronnen. Bedragen blijven in de opgegeven eenheid bewaard.</p>{message}
<section><h2>Actieve tarieven op {today}</h2>
<p class="hint">Variabele bedragen zijn levering plus belasting, inclusief btw. Vaste bedragen zijn
alleen voor deze kalenderdag omgerekend. Netbeheer staat apart. Regels met een verbruiksband
of jaarafrekening staan niet in deze eenvoudige optelling ({conditional} zulke regels).</p>
<div class="scroll"><table><tr><th>Soort</th><th>Variabel</th><th>Vaste levering</th><th>Belastingkorting</th><th>Netbeheer</th></tr>{summary}</table></div></section>
<section><h2>1. Contract van leverancier</h2><form method="post" action="/contract">{csrf}
<div class="grid"><label>Nutssoort<select name="commodity">{commodities}</select></label>
<label>Leverancier<input name="supplier" maxlength="120" required></label>
<label>Ingangsdatum<input name="valid_from" type="date" required></label>
<label>Omschrijving<input name="description" maxlength="200"></label></div>
<button>Contract opslaan</button></form><div class="scroll"><table><tr><th>Soort</th><th>Leverancier</th><th>Vanaf</th><th>Tot</th></tr>{contracts}</table></div></section>
<section><h2>2. Afrekenperiode stroom</h2>
<p class="hint">Leg per stroomcontract vast welke afrekenwijze tussen twee datums geldt.
De einddatum telt niet mee. Een volgende periode kan op die datum beginnen.</p>
<form method="post" action="/settlement">{csrf}<div class="grid">
<label>Stroomcontract<select name="contract_id" required>{electricity_contracts}</select></label>
<label>Begindatum<input name="starts_on" type="date" required></label>
<label>Einddatum<input name="ends_on" type="date" required></label>
<label>Afrekenwijze<select name="electricity_policy">{policies}</select></label></div>
<button>Periode opslaan</button></form><div class="scroll"><table><tr><th>Leverancier</th><th>Vanaf</th><th>Tot</th><th>Wijze</th></tr>{settlements}</table></div></section>
<section><h2>3. Netbeheer of overheid</h2><form method="post" action="/source">{csrf}
<div class="grid"><label>Nutssoort<select name="commodity">{commodities}</select></label>
<label>Bron<select name="source_kind">{source_kinds}</select></label>
<label>Naam<input name="source_name" maxlength="120" required></label></div>
<button>Bron opslaan</button></form><div class="scroll"><table><tr><th>Soort</th><th>Bron</th><th>Naam</th></tr>{sources}</table></div></section>
<section><h2>4. Tarief als concept</h2><p class="hint">Voer een regel per onderdeel in. Voorbeeld stroom normaal:
€0,15010 levering plus €0,11085 energiebelasting geeft €0,26095/kWh totaal.
Vul dat totaal niet nog eens als levering in. Vaste bedragen mogen per dag,
maand of jaar worden ingevoerd. Een nieuwe regel met dezelfde herkenningsnaam
sluit de vorige vanaf zijn ingangsdatum.</p>
<form method="post" action="/rule">{csrf}<div class="grid">
<label>Bron<select name="source_id" required>{source_options}</select></label>
<label>Herkenningsnaam<input name="code" placeholder="levering_normaal" required></label>
<label>Onderdeel<select name="component">{components}</select></label>
<label>Richting<select name="flow">{flows}</select></label>
<label>Telwerk<select name="register_code">{registers}</select></label>
<label>Bedrag in euro, negatief bij vergoeding<input name="amount_eur" inputmode="decimal" required></label>
<label>Eenheid<select name="basis">{bases}</select></label>
<label>Btw<select name="includes_vat"><option value="1">Bedrag inclusief btw</option><option value="0">Bedrag exclusief btw</option></select></label>
<label>Btw-tarief als fractie<input name="vat_rate" value="0.21" inputmode="decimal"></label>
<label>Ingangsdatum<input name="valid_from" type="date" required></label>
<label>Einddatum, meestal leeg<input name="valid_until" type="date"></label>
<label>Verbruiksband<select name="band_quantity">{bands}</select></label>
<label>Band vanaf<input name="band_from" inputmode="decimal"></label>
<label>Band tot<input name="band_until" inputmode="decimal"></label>
<label>Toelichting<input name="notes" maxlength="255"></label></div>
<button>Concept opslaan</button></form></section>
<section><h2>Tariefregels</h2><p class="hint">Controleer bron, bedrag, btw, eenheid en datum voordat je activeert.</p>
<div class="scroll"><table><tr><th>Bron</th><th>Onderdeel</th><th>Bedrag</th><th>Periode</th><th>Status</th><th></th></tr>{rules}</table></div></section>
</main></html>""".format(
        message='<p class="notice">{}</p>'.format(esc(message)) if message else "",
        csrf=csrf_field(), commodities=options(logic.COMMODITIES),
        today=esc(today), summary=summary_rows, conditional=len(conditional),
        source_kinds=options(logic.SOURCE_KINDS), contracts=contract_rows,
        electricity_contracts=electricity_contracts,
        policies=options(logic.POLICIES), settlements=settlement_rows,
        sources=source_rows, source_options=source_options,
        components=options(logic.COMPONENTS), flows=options(logic.FLOWS),
        registers=options(logic.REGISTERS), bases=options(logic.BASES),
        bands='<option value="">Geen</option>' + options(logic.BANDS[1:]),
        rules="".join(rule_rows))


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
        if urlsplit(self.path).path != "/":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        connection = None
        try:
            connection = database()
            notice = parse_qs(urlsplit(self.path).query).get("notice", [""])[0]
            self.html(HTTPStatus.OK, page(connection, notice[:200]))
        except Exception as exc:
            print("Tarievenpagina: {}".format(exc), flush=True)
            self.html(HTTPStatus.SERVICE_UNAVAILABLE, "Database tijdelijk niet bereikbaar")
        finally:
            if connection is not None:
                connection.close()

    def do_POST(self):
        if not self.require_auth():
            return
        path = urlsplit(self.path).path
        if path not in ("/contract", "/settlement", "/source", "/rule", "/activate"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        try:
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
                else:
                    logic.activate_rule(connection, logic.positive_id(form.get("id", ""), "Tariefregel"))
                    message = "Tariefregel geactiveerd; vorige regel met dezelfde naam afgesloten."
                connection.commit()
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", "/?notice=" + quote(message))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        except logic.InputError as exc:
            self.html(HTTPStatus.BAD_REQUEST, "<h1>Controleer invoer</h1><p>{}</p><p><a href='/'>Terug</a></p>".format(esc(exc)))
        except Exception as exc:
            print("Tariefopslag: {}".format(exc), flush=True)
            self.html(HTTPStatus.INTERNAL_SERVER_ERROR, "Opslaan mislukt; controleer de containerlog")


def main():
    if not os.getenv("TARIFF_ADMIN_USER") or not os.getenv("TARIFF_ADMIN_PASSWORD"):
        raise SystemExit("TARIFF_ADMIN_USER en TARIFF_ADMIN_PASSWORD ontbreken")
    port = 8765
    print("Tarievenbeheer op poort {}".format(port), flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
