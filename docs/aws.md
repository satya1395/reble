# AWS: Glue + S3

Run Reble as-is against your AWS account: Glue as the catalog, S3 as the
warehouse. This walkthrough takes about five minutes, has you run each
command yourself, and cleans up at the end. Nothing to deploy — the CLI
is the client.

## Prerequisites

1. **An AWS account** and a CLI profile with credentials you can use
   (`aws sts get-caller-identity` works). Attach the least-privilege
   policy — print it with the command below, or copy it from the
   collapsible block — to the user or role you'll use. (For this
   walkthrough, `Resource: "*"` on Glue is fine; the smoke's policy is
   scoped tighter.)

    ```bash
    python -m reble.aws_smoke --show-policy
    ```

    <details>
    <summary>Copy the policy from here</summary>

    ```json
    {
      "Version": "2012-10-17",
      "Statement": [
        {
          "Sid": "IdentifyAccount",
          "Effect": "Allow",
          "Action": "sts:GetCallerIdentity",
          "Resource": "*"
        },
        {
          "Sid": "SmokeBucket",
          "Effect": "Allow",
          "Action": [
            "s3:CreateBucket", "s3:DeleteBucket", "s3:ListBucket",
            "s3:PutObject", "s3:GetObject", "s3:DeleteObject",
            "s3:ListBucketMultipartUploads", "s3:AbortMultipartUpload"
          ],
          "Resource": [
            "arn:aws:s3:::reble-smoke-*",
            "arn:aws:s3:::reble-smoke-*/*"
          ]
        },
        {
          "Sid": "SmokeGlue",
          "Effect": "Allow",
          "Action": [
            "glue:CreateDatabase", "glue:GetDatabase", "glue:GetDatabases",
            "glue:DeleteDatabase", "glue:CreateTable", "glue:GetTable",
            "glue:GetTables", "glue:UpdateTable", "glue:DeleteTable"
          ],
          "Resource": "*"
        }
      ]
    }
    ```
    </details>

2. **Python 3.10+** and Reble with the AWS extra:

    ```bash
    pip install 'reble[aws]'
    ```

3. **A bucket you can write to**, and your profile's region (we'll use
   both consistently — for Glue, S3, and duckdb's streaming reads).

## Step 1: create the project

Run the commands below in your terminal — they create a `models/` folder
with two SQL files (the models). A *model* is one SQL file that produces
one table, and the file name is the table name.

```bash
mkdir reble-aws && cd reble-aws && mkdir models
cat > models/stg_orders.sql <<'SQLEOF'
-- kind: table
-- key: id
select * from raw_events where amount > 10
SQLEOF
cat > models/mart_orders.sql <<'SQLEOF'
-- kind: table
-- key: id
select id, amount, amount * 2 as amount_doubled from stg_orders
SQLEOF
```

Now create a file named `reble.yml` in the project folder with the
following content — replace `your-bucket` and `us-east-1` with your own:

```yaml title="reble.yml"
version: 1
warehouse:
  catalog:
    type: glue
    region: us-east-1            # your profile's region
    warehouse: s3://your-bucket/reble-tutorial
  namespace: analytics_tutorial  # the Glue database Reble will create
  default_base: main
branching:
  git_sync: false                # standalone tutorial; no git needed
lineage:
  models_path: models
```

Credentials flow from the standard AWS chain for **both layers** —
pyiceberg/boto3 for writes and catalog operations, and duckdb's
streaming reads (resolved automatically, never logged). No secrets in
`reble.yml`, ever.

## Step 2: seed one input table

Your models read from `raw_events` — that's the upstream input your
ingestion normally lands. Create a file named `seed.py` in the project
folder (same folder as `reble.yml`) with the following code — it creates
the table and appends three rows:

```python title="seed.py"
import pyarrow as pa, yaml
from pyiceberg.catalog import load_catalog

cfg = yaml.safe_load(open("reble.yml"))
cat = load_catalog("reble", **cfg["warehouse"]["catalog"])
cat.create_namespace_if_not_exists(cfg["warehouse"]["namespace"])
cat.create_table(
    "analytics_tutorial.raw_events",
    schema=pa.schema([("id", pa.int64()), ("amount", pa.float64())]),
).append(pa.table({"id": [1, 2, 3], "amount": [5.0, 20.0, 30.0]}))
```

```bash
export AWS_PROFILE=yourprofile
python seed.py
```

## Step 3: build production (once)

The goal of this walkthrough: **change a model, test the change safely,
and only then touch production.** Before you can change something, you
need a starting point — build once:

```bash
reble run --refresh
```

No model list needed: `--refresh` builds everything that isn't built
yet — on a first run, that's all of your models, whether you have two or
two hundred. Later it means something narrower (rebuild only what the
data touched), which is why this exact command is your nightly job.

That's it: two Iceberg tables now exist in your S3 bucket, registered in
Glue, built in the right order (`stg_orders` before `mart_orders` —
Reble read the dependency out of the SQL; you didn't configure it).
`main` — production — now has your tables.

## Step 4: a real change — production stays untouched

Tuesday morning. Finance says the revenue numbers look wrong: some
orders in the report are test orders with amounts above 100, and they
want them excluded. Your job: fix `stg_orders` and prove the fix before
it goes anywhere near production.

Edit `models/stg_orders.sql` and add the filter:

```sql
select * from raw_events where amount > 10 and amount <= 100
```

Now test it safely:

```bash
reble run
reble diff mart_orders
```

What just happened:

- Reble noticed your edit and rebuilt `stg_orders` **and every model
  built from it** (`mart_orders`) — on a **separate data branch**. Your
  `main` tables are exactly as they were. Finance's report is still
  running off the old numbers while you work; until you say otherwise,
  nothing here can affect production.
- Your branch builds on the data it started with: even if new orders
  land in `raw_events` right now, your test runs against the same input
  every time — so the diff shows *your fix's* effect, not the data
  moving underneath you.
- `reble diff` is the proof Finance asked for — what the fix removes,
  **as rows**:

```text
analytics_tutorial.mart_orders:  +0 -0 ~1  (keys: ['id'])
```

One order changed (the test order's doubled amount), keyed by `id`.
Paste that into the ticket and ask for sign-off — this is your review
artifact before anything reaches production.

(You didn't create the branch yourself — in this standalone tutorial the
change-set is simply `local`. In a git repo, each git branch gets its
own data branch automatically: `git switch -c fix-test-orders`, edit,
`reble run`.)

## Step 5: promote — apply when Finance signs off

Finance replies "looks right, ship it."

```bash
reble status     # did anything move underneath you while you waited?
reble promote    # apply the branch's tables to main
```

`promote` is the only step that touches `main` — and it's careful about
it: if new orders landed while you waited for sign-off, it rebuilds your
fix on the fresh data, shows you the new diff, and only then applies.
It never silently mixes your fix with data you never reviewed.

Had Finance said "no" — or never replied — you'd simply walk away.
`reble branch discard` deletes the branch and its pin tags; main never
knew the change existed.

## Step 6: clean up

```bash
aws glue delete-table --database-name analytics_tutorial --name raw_events
aws glue delete-table --database-name analytics_tutorial --name stg_orders
aws glue delete-table --database-name analytics_tutorial --name mart_orders
aws glue delete-database --name analytics_tutorial
aws s3 rm s3://your-bucket/reble-tutorial --recursive
```

Cost of the whole walkthrough: cents.

## Run this in production

The walkthrough ran on your laptop — fine for a tutorial, wrong for a
nightly job. In production the same commands run from whatever already
schedules your work. With Airflow, the refresh becomes one task at the
end of your ingestion DAG:

```python
ingest >> BashOperator(
    task_id="reble_refresh",
    bash_command="cd /srv/warehouse && reble run --refresh",
)
```

Install `reble[aws]` in the worker image, pass credentials the way you
already do (profile, role, or env vars — the standard chain), and the
refresh scopes itself from whatever the ingest just landed. The DAG
needs no branch or pin knowledge — that state lives in the catalog
(refs, tags, snapshots) and in `.reble/` on the worker, and the refresh
reads it at run time. The full pattern book — fail-mid-run behavior,
promotion as a gated deploy step, where state lives — is in the
[Airflow guide](airflow.md).

## Troubleshooting

- **`Unable to locate credentials` / `No region`** — the profile isn't
  loading: check `aws configure list` and `AWS_PROFILE`.
- **`AccessDenied` on Glue or S3** — attach the policy from the
  prerequisites.
- **Streaming fallback warnings (`iceberg_scan failed ... fell back`)** —
  duckdb couldn't read from S3; almost always region or credentials
  reaching duckdb differently than boto3. Set
  `engines.duckdb.settings.s3_region` explicitly and re-run.
