"""Read-only preview of one local day's consumption and source coverage."""

import argparse
import json
import os
from datetime import date

from .aggregate import build_daily, day_inputs, METRICS, COMMODITIES


def preview(connection, day):
    p1, water, pv = day_inputs(connection, day)
    calculated = build_daily(day, p1, water, pv)
    sources = {"p1": p1, "water": water, "pv": pv}
    ranges = {name: ({"first_utc": rows[0]["measured_at_utc"].isoformat() + "Z",
                      "last_utc": rows[-1]["measured_at_utc"].isoformat() + "Z"}
                     if rows else None) for name, rows in sources.items()}
    values = {metric: (str(calculated[metric]) if calculated[metric] is not None
                       else None) for metric in METRICS}
    quality = {commodity: calculated[commodity + "_quality"]
               for commodity in COMMODITIES}
    source = {commodity: calculated[commodity + "_source"]
              for commodity in COMMODITIES}
    return {"date": day.isoformat(),
            "source_rows": {name: len(rows) for name, rows in sources.items()},
            "source_range_utc": ranges,
            "values": values, "quality": quality, "source": source}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("day", type=date.fromisoformat, help="lokale dag YYYY-MM-DD")
    args = parser.parse_args()

    import mysql.connector

    connection = mysql.connector.connect(
        host=os.environ["DB_HOST"], port=int(os.getenv("DB_PORT", "3306")),
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
        database="nuts_v2", connection_timeout=5,
    )
    try:
        print(json.dumps(preview(connection, args.day), ensure_ascii=False))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
