"""Public read-only charts, independent from tariff administration."""

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import urlsplit

from .dashboard_data import snapshot


ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
          "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8")}


def database():
    import mysql.connector
    return mysql.connector.connect(
        host=os.environ["DB_HOST"], port=int(os.getenv("DB_PORT", "3306")),
        user=os.environ["NOTA_DB_USER"], password=os.environ["NOTA_DB_PASSWORD"],
        database="nuts_v2", connection_timeout=5, read_timeout=15,
    )


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/dashboard":
            try:
                connection = database()
                try:
                    result = snapshot(connection)
                finally:
                    connection.close()
                result["nota_port"] = int(os.getenv("NOTA_WEB_PORT", "8766"))
                body = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
                self.respond(HTTPStatus.OK, body, "application/json; charset=utf-8")
            except Exception as exc:
                print("Dashboard: {}".format(exc), flush=True)
                self.respond(HTTPStatus.SERVICE_UNAVAILABLE,
                             b'{"error":"Gegevens tijdelijk niet beschikbaar"}',
                             "application/json; charset=utf-8")
            return
        if path not in ASSETS:
            self.respond(HTTPStatus.NOT_FOUND, b"Niet gevonden", "text/plain; charset=utf-8")
            return
        name, content_type = ASSETS[path]
        body = files("nuts.dashboard_assets").joinpath(name).read_bytes()
        self.respond(HTTPStatus.OK, body, content_type)

    def do_POST(self):
        self.respond(HTTPStatus.METHOD_NOT_ALLOWED, b"Alleen lezen", "text/plain; charset=utf-8")


def main():
    if not os.getenv("NOTA_DB_USER") or not os.getenv("NOTA_DB_PASSWORD"):
        raise SystemExit("NOTA_DB_USER en NOTA_DB_PASSWORD ontbreken")
    print("Grafiekenpagina op poort 8767", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8767), Handler).serve_forever()


if __name__ == "__main__":
    main()
