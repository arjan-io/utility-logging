"""Read-only household invoice forecast on a separate Pi web port.

Only the forecast page is exposed. Tariff forms remain on the admin service,
which Compose binds to the Pi's loopback interface.
"""

import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import nota


PUBLIC_PATHS = frozenset(("/", "/nota", "/nota.php"))


def database():
    import mysql.connector
    return mysql.connector.connect(
        host=os.environ["DB_HOST"], port=int(os.getenv("DB_PORT", "3306")),
        user=os.environ["NOTA_DB_USER"], password=os.environ["NOTA_DB_PASSWORD"],
        database="nuts_v2", connection_timeout=5,
    )


def render(path, connect=database):
    """Resolve a public path without exposing the tariff administration."""
    if path not in PUBLIC_PATHS:
        return HTTPStatus.NOT_FOUND, "Niet gevonden"
    connection = connect()
    try:
        return HTTPStatus.OK, nota.page(connection, navigation="")
    finally:
        connection.close()


class Handler(BaseHTTPRequestHandler):
    def send_page(self, status, body):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            status, body = render(path)
        except Exception as exc:
            print("Notapagina: {}".format(exc), flush=True)
            status, body = HTTPStatus.SERVICE_UNAVAILABLE, "Nota tijdelijk niet beschikbaar"
        self.send_page(status, body)

    def do_POST(self):
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)


def main():
    if not os.getenv("NOTA_DB_USER") or not os.getenv("NOTA_DB_PASSWORD"):
        raise SystemExit("NOTA_DB_USER en NOTA_DB_PASSWORD ontbreken")
    print("Notapagina op poort 8766", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8766), Handler).serve_forever()


if __name__ == "__main__":
    main()
