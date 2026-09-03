# AWS: Glue + S3

Reble runs as-is against your AWS account: Glue as the catalog, S3 as the
warehouse. Nothing to deploy — the CLI is the client.

```bash
pip install 'reble[aws]'
export AWS_PROFILE=yourprofile        # or env keys / SSO — the standard chain
```

```yaml title="reble.yml"
version: 1
warehouse:
  catalog:
    type: glue
    region: us-east-1
    warehouse: s3://your-bucket/reble
  namespace: analytics
  default_base: main
lineage:
  models_path: models
```

From here every verb is the same as local: `reble run`, `reble diff`,
`reble promote`, `reble run --refresh` on a schedule.

## Credentials

Both layers read the standard AWS chain (`AWS_PROFILE`, environment keys,
SSO):

- **Writes and catalog operations** go through pyiceberg/boto3 — nothing to
  configure beyond the profile.
- **Streaming reads** (duckdb `iceberg_scan`) get their region and
  credentials **resolved automatically via boto3** and applied to duckdb —
  you don't duplicate secrets anywhere, and values are never logged or
  written to disk.

Manual override, if you need it: `engines.duckdb.settings` in `reble.yml`
takes full responsibility (nothing automatic is applied).

```yaml
engines:
  duckdb:
    settings:
      s3_region: us-east-1
      # s3_access_key_id / s3_secret_access_key / s3_session_token — or
      # anything else you'd SET in duckdb
```

## Verify your setup in five minutes

The repo ships a self-verifying smoke that creates a disposable bucket +
Glue database, runs the entire lifecycle, and cleans up after itself:

```bash
export AWS_PROFILE=yourprofile
python examples/aws-glue/smoke.py             # 100k-row lifecycle
python examples/aws-glue/smoke.py --pass-1m   # + a 1M-row pass
```

It fails loudly if streaming reads fall back to in-memory — so a green run
proves the full path: Glue catalog ops, S3 writes, `iceberg_scan` over S3,
drift detection, promote-with-rerun, refresh, gc. Cost is cents.

## Measured on real S3 (1M rows, us-east-1, laptop)

| Phase | Time |
| --- | --- |
| Branch operations (create/promote/discard) | seconds, metadata-only |
| Scoped run of 2 models over 1M-row input | ~13 s |
| Data-driven refresh (`--refresh`) after 1M new rows | ~13 s |
| Keyed diff of 1M-row table | ~4 s |
| Drift detection (`status`) | ~2 s |

Your numbers will vary with region and network; the shape is the point —
laptop, no cluster, real warehouse sizes.

## Permissions the profile needs

- `sts:GetCallerIdentity` (identify the account)
- Glue: create/get/update/delete on the databases and tables Reble manages
- S3: read/write/list/delete on the warehouse prefix (plus
  `s3:CreateBucket`/`DeleteBucket` if you use the smoke's disposable bucket)
