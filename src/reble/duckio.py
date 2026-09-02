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
  settings: {k: v}             (raw SET passthrough — e.g. s3 credentials
                                for remote reads)
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
        return con

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
                    con.execute(
                        f'CREATE VIEW "{view}" AS SELECT * FROM iceberg_scan(?, '
                        f"snapshot_from_id={int(snapshot_id)}, allow_moved_paths=true)",
                        [location],
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
