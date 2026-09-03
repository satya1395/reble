# AWS: Glue + S3

Run Reble against your AWS account: Glue as the catalog, S3 as the
warehouse. Nothing to deploy — the CLI is the client. This walkthrough
takes about ten minutes from a clean machine and cleans up at the end.

## Prerequisites

### 1. Python 3.10–3.13

Python 3.14 is not yet tested. Check with `python3 --version`. If you're
on 3.14, create a 3.13 venv:

```bash
python3.13 -m venv reble-test && source reble-test/bin/activate
```

### 2. Install Reble (the right version)

```bash
pip install 'reble[aws]==0.5.0'
reble --version    # must say 0.5.0 — if it says 0.1.1, force-reinstall:
# pip install --force-reinstall 'reble[aws]==0.5.0'
```

### 3. An AWS profile that works

Verify credentials and region are configured:

```bash
aws configure list          # should show an access key and a region
aws sts get-caller-identity # should return your account ID
```

If not, set them up:

```bash
aws configure set aws_access_key_id YOUR_KEY --profile orchestra
aws configure set aws_secret_access_key YOUR_SECRET --profile orchestra
aws configure set region us-east-1 --profile orchestra
```

Export for this session:

```bash
export AWS_PROFILE=orchestra
export AWS_DEFAULT_REGION=us-east-1
```

### 4. An S3 bucket you can write to

If you don't have one, create it:

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
aws s3api create-bucket --bucket "reble-test-${ACCOUNT_ID}" --region us-east-1
echo "bucket: reble-test-${ACCOUNT_ID}"
```

Note the bucket name — you'll put it in the config below.

## Step 1: create the project

```bash
mkdir my-warehouse && cd my-warehouse && mkdir models
```

Create two SQL files — a *model* is one SQL file that produces one
table, and the file name is the table name:

```bash
cat > models/stg_orders.sql <<'EOF'
-- kind: table
-- key: order_id
select * from raw_events where amount > 10
EOF

cat > models/mart_orders.sql <<'EOF'
-- kind: table
-- key: order_id
select order_id, amount, amount * 2 as amount_doubled from stg_orders
EOF
```

Now create `reble.yml` — **replace `reble-test-YOUR_ACCOUNT_ID` with your
bucket name**:

```yaml title="reble.yml"
version: 1
warehouse:
  catalog:
    type: glue
    region: us-east-1
    warehouse: s3://reble-test-YOUR_ACCOUNT_ID/reble
  namespace: analytics_test
  default_base: main
branching:
  git_sync: false
lineage:
  models_path: models
```

## Step 2: seed one input table

Your models read from `raw_events` — that's the upstream input your
ingestion normally lands. Create a file named `seed.py` in the project
folder:

```python title="seed.py"
import os

import pyarrow as pa
import yaml
from pyiceberg.catalog import load_catalog

cfg = yaml.safe_load(open("reble.yml"))
cat_cfg = dict(cfg["warehouse"]["catalog"])
# pyiceberg reads glue.region, not region (fixed in 0.5.1)
if "region" in cat_cfg:
    cat_cfg["glue.region"] = cat_cfg.pop("region")

cat = load_catalog("reble", **cat_cfg)
ns = cfg["warehouse"]["namespace"]
cat.create_namespace_if_not_exists(ns)
cat.create_table(
    f"{ns}.raw_events",
    schema=pa.schema([("order_id", pa.int64()), ("amount", pa.float64())]),
).append(pa.table({
    "order_id": [1, 2, 3, 4, 5, 6],
    "amount": [5.0, 15.0, 25.0, 8.0, 30.0, 12.0],
}))
print(f"seeded {ns}.raw_events: 6 rows")
```

Run it:

```bash
python seed.py
```

If you get `ACCESS_DENIED`, your profile doesn't have S3 write access to
the bucket — check the bucket policy or use a different bucket.

## Step 3: build production (once)

```bash
reble run --refresh
```

No model list needed — `--refresh` builds everything that isn't built
yet. On a first run, that's all of your models, whether you have two or
two hundred. You'll see:

```
scope (edited)       2   mart_orders, stg_orders
pinned inputs        1   reble_pin__local__raw_events
engine                 duckdb (local)
stg_orders: ran (4 rows, ...)
mart_orders: ran (4 rows, ...)
```

Two Iceberg tables now exist in your S3 bucket, registered in Glue,
built in the right order (Reble read the dependency from the SQL — you
didn't configure it). `main` — production — now has your tables.

## Step 4: change a model — production stays untouched

Now the part you came for. Edit `models/stg_orders.sql`: change
`amount > 10` to `amount > 25`. Then:

```bash
reble run
reble diff mart_orders
```

Reble noticed your edit and rebuilt that model **and every model built
from it** — on a **separate data branch**. Your `main` tables are
exactly as they were. Don't like the change? Edit again, or walk away —
main never knew.

`reble diff` shows what your change produced — **rows, not SQL**:

```
analytics_test.mart_orders:  +2 -0 ~0  (keys: ['order_id'])
```

This is your review artifact before anything reaches production.

## Step 5: promote

```bash
reble status     # did anything move underneath you while you worked?
reble promote    # apply the branch's tables to main
```

`promote` is the only step that touches `main` — and it's careful about
it: if the input data changed under your feet, it rebuilds your change
on the fresh data, shows you the new diff, and only then applies. It
never silently mixes your change with someone else's data.

## Step 6: clean up

```bash
aws glue delete-table --database-name analytics_test --name raw_events
aws glue delete-table --database-name analytics_test --name stg_orders
aws glue delete-table --database-name analytics_test --name mart_orders
aws glue delete-database --name analytics_test
aws s3 rm s3://reble-test-${ACCOUNT_ID}/reble --recursive
```

Cost of the whole walkthrough: cents.

## Run it in production

The walkthrough ran on your laptop — fine for a tutorial, wrong for a
nightly job. In production the same commands run from whatever already
schedules your work. With Airflow, the refresh becomes one task:

```python
ingest >> BashOperator(
    task_id="reble_refresh",
    bash_command="cd /srv/warehouse && reble run --refresh",
)
```

Install `reble[aws]` in the worker image, pass credentials the way you
already do, and the refresh scopes itself from whatever the ingest just
landed. The full pattern book is in the [Airflow guide](airflow.md).

## Troubleshooting

- **`reble --version` says 0.1.1** — old cached package. Force-reinstall:
  `pip install --force-reinstall 'reble[aws]==0.5.0'`
- **`You must specify a region`** — set `export AWS_DEFAULT_REGION=us-east-1`
  (the `region` key in reble.yml is fixed in 0.5.1; until then, the env
  var is the reliable path)
- **`Unable to locate credentials`** — `export AWS_PROFILE=orchestra` and
  verify with `aws sts get-caller-identity`
- **`ACCESS_DENIED` on S3** — the bucket policy doesn't allow your IAM
  user/role to write. Use a bucket you own or update the policy.
- **`input 'raw_events' not found`** — run `seed.py` first (Step 2)
- **`No such option '--refresh'`** — you have the old package version;
  see the first item above.
