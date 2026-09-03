#!/usr/bin/env bash
# End-to-end demo of the reble loop against a local Iceberg catalog.
# Usage: ./demo.sh          (run from the repo root; needs `pip install reble`)
# Wipes and recreates ./demo/ — safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

ROOT="$PWD"
DEMO="$ROOT/demo"
export DEMO

command -v reble >/dev/null 2>&1 || {
  echo "reble not found on PATH — pip install reble (or pip install -e .)" >&2
  exit 1
}
PYTHON="${PYTHON:-python3}"

rm -rf "$DEMO"
mkdir -p "$DEMO"/{models,warehouse}
cd "$DEMO"
git init -q -b main
git config user.email demo@local && git config user.name demo

# --- project: 2 models, one raw input table on main -------------------
cat > models/stg_orders.sql <<'EOF'
-- kind: table
-- key: order_id
select * from raw_events where amount > 15
EOF

cat > models/mart_orders.sql <<'EOF'
-- kind: table
-- key: order_id
select order_id, amount * 2 as amount_doubled from stg_orders
EOF

cat > reble.yml <<EOF
version: 1
warehouse:
  catalog:
    type: sql
    uri: sqlite:///$DEMO/catalog.db
    warehouse: file://$DEMO/warehouse
  namespace: analytics
  default_base: main
lineage:
  models_path: models
  dialect: duckdb
EOF
echo ".reble/" > .gitignore
git add -A && git commit -qm init

# --- seed raw_events on "main" (3 rows) --------------------------------
"$PYTHON" - <<'PY'
import os

import pyarrow as pa
from pyiceberg.catalog import load_catalog

demo = os.environ["DEMO"]
cat = load_catalog(
    "reble", type="sql",
    uri=f"sqlite:///{demo}/catalog.db",
    warehouse=f"file://{demo}/warehouse",
)
cat.create_namespace_if_not_exists("analytics")
t = cat.create_table(
    "analytics.raw_events",
    schema=pa.schema([("order_id", pa.int64()), ("amount", pa.float64())]),
)
t.append(pa.table({"order_id": [1, 2, 3], "amount": [10.0, 20.0, 30.0]}))
print("seeded analytics.raw_events: 3 rows on main")
PY

# --- the reble loop -----------------------------------------------------
step() { echo; echo "==> $*"; }

step "git switch -c fix-orders  (then edit the model)"
git switch -c fix-orders
"$PYTHON" - <<'PY'
from pathlib import Path

p = Path("models/stg_orders.sql")
p.write_text(p.read_text().replace("amount > 15", "amount > 5"))
print("edited: stg_orders filter 15 -> 5")
PY

step "reble run --dry-run"
reble run --dry-run

step "reble run"
reble run

step "reble diff stg_orders   (keyed on order_id)"
reble --json diff stg_orders | "$PYTHON" -c '
import json, sys
t = json.load(sys.stdin)["data"]["tables"][0]
print("+{} -{} ~{} (keys={})".format(
    t["added"], t["removed"], t["changed"], t["key_columns"]))
print("  samples:", t["samples"]["added"])
'

step "reble status   (clean -> exit 0)"
reble status; echo "exit=$?"

step "someone writes to main..."
"$PYTHON" - <<'PY'
import os

import pyarrow as pa
from pyiceberg.catalog import load_catalog

demo = os.environ["DEMO"]
cat = load_catalog(
    "reble", type="sql",
    uri=f"sqlite:///{demo}/catalog.db",
    warehouse=f"file://{demo}/warehouse",
)
cat.load_table("analytics.raw_events").append(
    pa.table({"order_id": [4], "amount": [40.0]})
)
print("raw_events: appended order_id=4 on main")
PY

step "reble status   (drift -> exit 3)"
reble status || echo "exit=$?"

step "reble promote --ff-only   (blocked -> exit 4)"
reble promote --ff-only || echo "exit=$?"

step "reble promote   (re-run + fresh diff + fast-forward)"
reble promote

step "verify main got everything"
"$PYTHON" - <<'PY'
import os

from pyiceberg.catalog import load_catalog

demo = os.environ["DEMO"]
cat = load_catalog(
    "reble", type="sql",
    uri=f"sqlite:///{demo}/catalog.db",
    warehouse=f"file://{demo}/warehouse",
)
for t in ("stg_orders", "mart_orders"):
    rows = cat.load_table(f"analytics.{t}").scan().to_arrow().to_pylist()
    print(f"analytics.{t}: {len(rows)} rows on main -> {rows}")
PY

step "cleanup branch, then done (demo/ left for inspection)"
reble branch discard fix-orders --yes
echo
echo "demo complete — catalog db, warehouse files and .reble/ state are under $DEMO"
