"""Seed the example's upstream input table on main: analytics.raw_events.

Run AFTER `reble init` (which writes reble.yml with a local sql catalog):

    python seed.py

Creates three orders in the analytics.raw_events table — the "production"
input your models read from. Safe to re-run only on a fresh catalog.
"""

import pyarrow as pa
import yaml
from pyiceberg.catalog import load_catalog


def main() -> None:
    with open("reble.yml") as f:
        cfg = yaml.safe_load(f)
    cat_cfg = cfg["warehouse"]["catalog"]
    catalog = load_catalog("reble", **cat_cfg)
    namespace = cfg["warehouse"]["namespace"]
    catalog.create_namespace_if_not_exists(namespace)

    table = catalog.create_table(
        f"{namespace}.raw_events",
        schema=pa.schema([
            ("order_id", pa.int64()),
            ("user_id", pa.int64()),
            ("status", pa.string()),
            ("amount", pa.float64()),
            ("event_ts", pa.timestamp("us")),
        ]),
    )
    table.append(
        pa.table({
            "order_id": [1, 2, 3],
            "user_id":  [101, 102, 103],
            "status":   ["paid", "paid", "paid"],
            "amount":   [10.0, 20.0, 30.0],
            "event_ts": [
                __import__("datetime").datetime(2026, 8, 30, 14, 22, 5),
                __import__("datetime").datetime(2026, 8, 31, 9, 14, 33),
                __import__("datetime").datetime(2026, 9, 1, 18, 3, 47),
            ],
        })
    )
    rows = catalog.load_table(f"{namespace}.raw_events").scan().to_arrow()
    print(f"seeded {namespace}.raw_events: {rows.num_rows} rows on main")


if __name__ == "__main__":
    main()
