"""Reble × AWS smoke: the full lifecycle on Glue + S3, then teardown.

The quickstart and the integration test are the same artifact. Run with a
profile that can create a bucket, use Glue, and write objects:

    export AWS_PROFILE=orchestra
    python examples/aws-glue/smoke.py                 # ~100k rows
    python examples/aws-glue/smoke.py --pass-1m       # + a 1M-row pass
    python examples/aws-glue/smoke.py --keep          # don't tear down

Creates a disposable bucket (reble-smoke-<account-id>) and Glue database
(reble_smoke); deletes both on exit unless --keep. The core assertion:
iceberg_scan streaming reads engage over S3 — any fallback to in-memory
reads fails the smoke.
"""

from __future__ import annotations

import argparse
import random
import shutil
import tempfile
import time
from pathlib import Path

import pyarrow as pa

from reble.core import Reble
from reble.errors import RebleError

NAMESPACE = "reble_smoke"

SMOKE_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "IdentifyAccount",
            "Effect": "Allow",
            "Action": "sts:GetCallerIdentity",
            "Resource": "*",
        },
        {
            "Sid": "SmokeBucket",
            "Effect": "Allow",
            "Action": [
                "s3:CreateBucket",
                "s3:DeleteBucket",
                "s3:ListBucket",
                "s3:PutObject",
                "s3:GetObject",
                "s3:DeleteObject",
                "s3:ListBucketMultipartUploads",
                "s3:AbortMultipartUpload",
            ],
            "Resource": [
                "arn:aws:s3:::reble-smoke-*",
                "arn:aws:s3:::reble-smoke-*/*",
            ],
        },
        {
            "Sid": "SmokeGlue",
            "Effect": "Allow",
            "Action": [
                "glue:CreateDatabase",
                "glue:GetDatabase",
                "glue:GetDatabases",
                "glue:DeleteDatabase",
                "glue:CreateTable",
                "glue:GetTable",
                "glue:GetTables",
                "glue:UpdateTable",
                "glue:DeleteTable",
            ],
            "Resource": "*",
        },
    ],
}
FALLBACK_MARK = "fell back to in-memory read"
all_warnings: list[str] = []


def phase(label: str):
    print(f"\n=== {label}")
    start = time.time()
    return lambda: print(f"    ({time.time() - start:.1f}s)")


def check(label: str, fn):
    """Run a lifecycle step; collect warnings; assert ok."""
    result = fn()
    warnings = result.get("warnings", [])
    all_warnings.extend(warnings)
    ok = result.get("ok", True)
    print(f"  {label}: {'ok' if ok else 'FAILED'}"
          + (f" | warnings: {warnings}" if warnings else ""))
    if not ok:
        raise SystemExit(f"smoke failed at: {label}")
    return result


def expect_error(label: str, fn, code: int):
    try:
        fn()
    except RebleError as exc:
        assert exc.exit_code == code, f"{label}: exit {exc.exit_code} != {code}"
        print(f"  {label}: ok (code {code}, as expected)")
        if exc.payload:
            all_warnings.extend(exc.payload.get("warnings", []))
        return
    raise SystemExit(f"smoke failed at: {label}: expected error {code}")


def aws_session():
    import boto3

    return boto3.session.Session()


def setup(account_id: str, region: str) -> tuple:
    """Bucket + Glue database + project dir. Returns (s3, bucket_name, workdir)."""
    session = aws_session()
    s3 = session.client("s3", region_name=region)
    bucket = f"reble-smoke-{account_id}"
    try:
        s3.head_bucket(Bucket=bucket)
        print(f"bucket {bucket} exists (reusing)")
    except Exception:  # noqa: BLE001 — any head_bucket miss means "create"
        s3.create_bucket(
            Bucket=bucket,
            **({"CreateBucketConfiguration": {"LocationConstraint": region}}
               if region != "us-east-1" else {}),
        )
        print(f"bucket {bucket} created in {region}")

    import boto3

    glue = boto3.client("glue", region_name=region)
    try:
        glue.create_database(DatabaseInput={"Name": NAMESPACE})
        print(f"glue database {NAMESPACE} created")
    except glue.exceptions.AlreadyExistsException:
        print(f"glue database {NAMESPACE} exists (reusing)")

    workdir = Path(tempfile.mkdtemp(prefix="reble-aws-smoke-"))
    (workdir / "models").mkdir()
    (workdir / "models" / "stg_orders.sql").write_text(
        "-- kind: table\n-- key: id\nselect * from raw_events where amount > 10\n"
    )
    (workdir / "models" / "mart_orders.sql").write_text(
        "-- kind: table\n-- key: id\n"
        "select id, amount, amount * 2 as amount_doubled from stg_orders\n"
    )
    (workdir / "reble.yml").write_text(f"""
version: 1
warehouse:
  catalog:
    type: glue
    region: {region}
    warehouse: s3://{bucket}/reble-smoke
  namespace: {NAMESPACE}
  default_base: main
branching:
  git_sync: false
lineage:
  models_path: models
  dialect: duckdb
""")
    return s3, bucket, workdir


def seed_rows(rows: int, start_id: int = 0) -> pa.Table:
    rng = random.Random(7)
    return pa.table(
        {
            "id": pa.array(range(start_id, start_id + rows), type=pa.int64()),
            "amount": pa.array([rng.uniform(0, 100) for _ in range(rows)],
                               type=pa.float64()),
        }
    )


def lifecycle(core: Reble, rows: int) -> None:
    cat = core.catalog
    raw = f"{NAMESPACE}.raw_events"

    t = phase(f"seed raw_events ({rows:,} rows)")
    if not cat.table_exists(raw):
        cat.create_table(
            raw, schema=pa.schema([("id", pa.int64()), ("amount", pa.float64())])
        )
    cat.load_table(raw).append(seed_rows(rows))
    t()

    t = phase("first run (models on main)")
    check("run --models", lambda: core.run(models=["stg_orders", "mart_orders"]))
    t()

    t = phase("edit + scoped re-run")
    stg = core.project_root / "models" / "stg_orders.sql"
    stg.write_text(
        "-- kind: table\n-- key: id\nselect * from raw_events where amount > 25\n"
    )
    core = Reble(core.project_root)
    check("run (inferred scope)", lambda: core.run())
    t()

    t = phase("diff")
    diff = check("diff mart_orders", lambda: core.diff(tables=["mart_orders"]))
    table = diff["data"]["tables"][0]
    assert table["added"] > 0, "expected rows in the diff"
    t()

    t = phase("drift: new data on main")
    cat.load_table(raw).append(seed_rows(1_000, start_id=10_000_000))
    expect_error("status (drift)", lambda: core.status(), code=3)
    t()

    t = phase("promote (re-run path) + refresh no-op")
    check("promote", lambda: core.promote())
    again = check("run --refresh (quiet)", lambda: core.run(refresh=True))
    assert again["data"].get("status", "").startswith("empty scope"), (
        "refresh after promote should be quiet"
    )
    t()

    t = phase("estimate")
    est = check("estimate", lambda: core.estimate())
    # after promote everything is quiet: either a priced scope or empty
    assert ("est_bytes_read" in est["data"]) or est["data"].get(
        "status", ""
    ).startswith("empty scope")
    t()

    t = phase("housekeeping")
    branch = diff["branch"]["data"]
    check("branch discard", lambda: core.branch_discard(branch))
    check("gc", lambda: core.gc())
    t()


def scale_pass(core: Reble) -> None:
    cat = core.catalog
    raw = f"{NAMESPACE}.raw_events"
    t = phase("1M pass: append to raw_events")
    table = cat.load_table(raw)
    for start in range(1_000_000, 1_000_000 * 2, 250_000):
        table.append(seed_rows(250_000, start_id=start))
    t()

    t = phase("1M pass: refresh (data-driven scope)")
    check("run --refresh", lambda: core.run(refresh=True))
    t()

    t = phase("1M pass: diff")
    check("diff mart_orders", lambda: core.diff(tables=["mart_orders"]))
    t()


def teardown(s3, bucket: str, region: str, workdir: Path) -> None:
    print("\n=== teardown")
    import boto3

    glue = boto3.client("glue", region_name=region)
    tables = glue.get_tables(DatabaseName=NAMESPACE).get("TableList", [])
    for t in tables:
        name = t["Name"]
        try:
            glue.delete_table(DatabaseName=NAMESPACE, Name=name)
            print(f"  glue table {name} deleted")
        except Exception as exc:  # noqa: BLE001
            print(f"  glue table {name}: {exc}")

    # purge objects under the smoke prefix
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="reble-smoke/"):
        objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        while objects:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objects[:1000]})
            objects = objects[1000:]
    print("  s3 prefix purged")

    try:
        s3.delete_bucket(Bucket=bucket)
        print(f"  bucket {bucket} deleted")
    except Exception as exc:  # noqa: BLE001
        print(f"  bucket delete skipped: {exc}")

    try:
        glue.delete_database(Name=NAMESPACE)
        print(f"  glue database {NAMESPACE} deleted")
    except Exception as exc:  # noqa: BLE001
        print(f"  database delete: {exc}")

    shutil.rmtree(workdir, ignore_errors=True)
    print("  workdir removed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--pass-1m", action="store_true")
    parser.add_argument("--keep", action="store_true", help="skip teardown")
    parser.add_argument("--profile", help="AWS profile name (else AWS_PROFILE/env)")
    parser.add_argument("--show-policy", action="store_true",
                        help="print the least-privilege IAM policy for this smoke and exit")
    args = parser.parse_args()

    if args.show_policy:
        import json

        print(json.dumps(SMOKE_POLICY, indent=2))
        return

    if args.profile:
        import os

        os.environ["AWS_PROFILE"] = args.profile

    session = aws_session()
    account = session.client("sts").get_caller_identity()["Account"]
    region = session.region_name
    if not region:
        raise SystemExit("no region resolved — set one in the profile or env")
    print(f"account …{account[-4:]} | region {region} | profile "
          f"{session.profile_name!r}")

    s3, bucket, workdir = setup(account, region)
    try:
        core = Reble(workdir)
        print(f"catalog: glue | warehouse: s3://{bucket}/reble-smoke | "
              f"namespace: {NAMESPACE}")
        lifecycle(core, args.rows)
        if args.pass_1m:
            scale_pass(Reble(workdir))
    finally:
        if not args.keep:
            teardown(s3, bucket, region, workdir)
        else:
            print(f"\n--keep: bucket {bucket}, database {NAMESPACE}, "
                  f"workdir {workdir} retained")

    fallbacks = [w for w in all_warnings if FALLBACK_MARK in w]
    print(f"\nwarnings collected: {len(all_warnings)} "
          f"(iceberg_scan fallbacks: {len(fallbacks)})")
    if fallbacks:
        for w in fallbacks:
            print(f"  FALLBACK: {w}")
        raise SystemExit("SMOKE FAILED: streaming reads fell back over S3")
    print("SMOKE PASSED")


if __name__ == "__main__":
    main()
