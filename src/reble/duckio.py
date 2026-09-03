"""DuckDB connection + lazy Iceberg snapshot views (DECISIONS §18-19).

Reads stream through duckdb's `iceberg_scan` (out-of-core, spills under
`memory_limit`) instead of materializing via pyiceberg → arrow. Correctness
is pinned to the catalog-committed snapshot id resolved from refs/tags —
never to whichever metadata file a glob finds: snapshots are immutable and
cumulative, so a pinned id reads identically regardless of the metadata
version that enumerates it (this is why `unsafe_enable_version_guessing` is
acceptable here and only here).

`engines.duckdb` config keys:
  read_mode: auto | arrow      (default auto: iceberg_scan with per-table
                                fallback to arrow)
  memory_limit: e.g. "4GB"     (duckdb spills to temp_directory beyond it)
  temp_directory: path         (default .reble/spill)
  settings: {k: v}             (raw SET passthrough — overrides S3
                                auto-configuration below)

S3 credentials: when a scan location is s3:// and the user hasn't provided
`settings`, region + credentials are resolved via boto3 — which honors
AWS_PROFILE, environment keys, and SSO — and applied to duckdb. Values are
never logged or persisted. Precedence: manual `settings` → boto3-resolved →
duckdb's own `LOAD aws` credential chain.
"""

from __future__ import annotations

from pathlib import Path

import duckdb


class DuckIo:
    """Connection factory + snapshot view registration for one project."""

    def __init__(self, duck_cfg: dict, reble_dir: Path | None = None):
        self.cfg = duck_cfg or {}
        self.reble_dir = reble_dir
        self.read_mode = self.cfg.get("read_mode", "auto")
        self.iceberg_ok = self.read_mode == "auto" and self._probe_iceberg()

    # ---------------------------------------------------------------- setup

    def _probe_iceberg(self) -> bool:
        try:
            con = duckdb.connect(":memory:")
            con.execute("LOAD iceberg;")
            con.close()
            return True
        except Exception:  # noqa: BLE001 — extension missing/offline → arrow
            return False

    def connect(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(":memory:")
        if self.iceberg_ok:
            con.execute("LOAD iceberg;")
            # Acceptable only because callers pin snapshot ids resolved from
            # the committed catalog state (see module docstring).
            con.execute("SET unsafe_enable_version_guessing=true;")
        if limit := self.cfg.get("memory_limit"):
            con.execute(f"SET memory_limit='{limit}';")
        temp = self.cfg.get("temp_directory")
        if temp is None and self.reble_dir is not None:
            temp = str(self.reble_dir / "spill")
        if temp:
            Path(temp).mkdir(parents=True, exist_ok=True)
            con.execute(f"SET temp_directory='{temp}';")
        for key, value in (self.cfg.get("settings") or {}).items():
            con.execute(f"SET {key}='{value}';")
        self._configure_s3(con)
        return con

    def _configure_s3(self, con: duckdb.DuckDBPyConnection) -> None:
        """Resolve region + credentials via boto3 and apply them to duckdb.

        Manual `settings` win — anything the user set is left untouched.
        Never logs values.
        """
        if self.cfg.get("settings"):
            return  # explicit settings take full responsibility
        resolved = _boto3_s3_credentials()
        if resolved is None:
            # last resort: duckdb's own credential chain
            try:
                con.execute("LOAD aws;")
                con.execute("CALL load_aws_credentials();")
            except Exception:  # noqa: BLE001 — local-only setups don't need s3
                pass
            return
        region, creds = resolved
        con.execute("LOAD httpfs;")
        if region:
            con.execute(f"SET s3_region='{region}';")
        con.execute(f"SET s3_access_key_id='{creds.access_key}';")
        con.execute(f"SET s3_secret_access_key='{creds.secret_key}';")
        if creds.token:
            con.execute(f"SET s3_session_token='{creds.token}';")

    # ----------------------------------------------------------- snapshot io

    def register_snapshot(
        self,
        con: duckdb.DuckDBPyConnection,
        view: str,
        table,
        snapshot_id: int | None,
        warnings: list[str] | None = None,
    ) -> str:
        """Register `view` over the table at a snapshot, lazily when possible.

        Returns the mode used: "iceberg_scan" or "arrow". Falls back to an
        arrow materialization per table on any extension failure.
        """
        if self.iceberg_ok and snapshot_id is not None:
            location = _scan_location(table)
            if location:
                try:
                    # The location must be inlined, not a prepared parameter:
                    # iceberg_scan cannot be prepared inside CREATE VIEW.
                    literal = location.replace("'", "''")
                    con.execute(
                        f'CREATE VIEW "{view}" AS SELECT * FROM iceberg_scan(\'{literal}\', '
                        f"snapshot_from_id={int(snapshot_id)}, allow_moved_paths=true)"
                    )
                    return "iceberg_scan"
                except Exception as exc:  # noqa: BLE001 — fall back per table
                    if warnings is not None:
                        warnings.append(
                            f"iceberg_scan failed for {location} "
                            f"({exc}); fell back to in-memory read"
                        )
        data = (
            table.scan(snapshot_id=snapshot_id).to_arrow()
            if snapshot_id is not None
            else table.scan().to_arrow()
        )
        con.register(view, data)
        return "arrow"


def _scan_location(table) -> str | None:
    """Table location as a duckdb-scannable path/URI (None if unavailable)."""
    try:
        location = table.location()
    except Exception:  # noqa: BLE001
        return None
    if not location:
        return None
    if location.startswith("file://"):
        return location[len("file://"):]
    return location


_S3_CRED_CACHE: list = []  # [(region, creds)] — resolved once per process


def _boto3_s3_credentials():
    """(region, credentials) from boto3's default chain, cached per process.

    Honors AWS_PROFILE, environment keys, SSO. Refreshable credential
    objects stay refreshable through the cache. Returns None when boto3 is
    unavailable or no credentials resolve (pure-local setups).
    """
    if _S3_CRED_CACHE:
        return _S3_CRED_CACHE[0]
    try:
        import boto3

        session = boto3.session.Session()
        region = session.region_name
        creds = session.get_credentials()
        if creds is None:
            return None
    except Exception:  # noqa: BLE001 — boto3 absent (extra not installed)
        return None
    _S3_CRED_CACHE.append((region, creds))
    return _S3_CRED_CACHE[0]
