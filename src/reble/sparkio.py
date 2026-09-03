"""Spark session + Iceberg snapshot views (the SparkEngine I/O layer).

Reads are pinned to catalog-committed snapshot ids via Spark's version
travel: `SELECT * FROM tbl VERSION AS OF <snapshot_id>` — the Spark
equivalent of duckdb's `iceberg_scan(snapshot_from_id=...)`. Metadata-only,
out-of-core, no arrow materialization.

Writes go through Iceberg's SQL extensions: `INSERT OVERWRITE tbl.branch_x`
(branch identifiers; the branch must already exist — Reble creates it via
pyiceberg in `catalog.py`, the same as the DuckDB path). Provenance rides
the snapshot summary through session conf:
`spark.sql.iceberg.snapshot-property.<key>` applies to the next write's
snapshot summary and is cleared after.

Catalog mapping — reble.yml catalog type → Spark catalog impl. Both pyiceberg
and Spark must point at the *same* catalog backend so refs are shared:

    glue                 → org.apache.iceberg.aws.glue.GlueCatalog
    hive                 → org.apache.iceberg.hive.HiveCatalog
    rest/polaris/nessie  → org.apache.iceberg.rest.RESTCatalog
    sql                  → org.apache.iceberg.jdbc.JdbcCatalog
                           (the JDBC catalog standard `iceberg_tables` schema
                           is what pyiceberg's SqlCatalog writes; a
                           `sqlite:///...` uri is translated to jdbc:sqlite:)

`engines.spark` config keys:
    master: e.g. "local[*]"  (default local[*] — no cluster required)
    app_name: str            (default "reble")
    settings: {k: v}         (raw Spark conf passthrough — wins over the
                              derived catalog config above; `packages` here
                              overrides the default runtime jar coordinate)
"""

from __future__ import annotations

import warnings

from .config import CatalogConfig
from .errors import ConfigError

ICEBERG_SPARK_RUNTIME = "org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.6.1"

CATALOG_TYPES = {
    # reble.yml catalog type → Iceberg `type=` for the SparkCatalog wrapper
    "glue": "glue",
    "hive": "hive",
    "rest": "rest",
    "polaris": "rest",
    "nessie": "rest",
    "reble": "rest",
    "sql": "jdbc",  # pyiceberg SqlCatalog and Iceberg JdbcCatalog share the
    #                `iceberg_tables` schema, so Spark and pyiceberg can
    #                point at the same database and share refs
}

GLUE_JARS = "org.apache.iceberg:iceberg-aws-bundle:1.6.1"


class SparkIo:
    """SparkSession factory + snapshot view registration for one project."""

    def __init__(self, spark_cfg: dict | None, catalog_cfg: CatalogConfig):
        self.cfg = spark_cfg or {}
        self.catalog_cfg = catalog_cfg
        self.catalog_name = self._catalog_name()
        self._session = None

    # ---------------------------------------------------------------- setup

    def _catalog_name(self) -> str:
        extra = self.catalog_cfg.model_extra or {}
        return str(extra.get("name") or "reble")

    def connect(self):
        """Get or build the SparkSession with the Reble catalog registered."""
        if self._session is not None:
            return self._session
        try:
            from pyspark.sql import SparkSession
        except ImportError as exc:  # pragma: no cover - extra not installed
            raise ConfigError(
                "pyspark is not installed — run `pip install 'reble[spark]'` "
                "or use compute_policy.prefer: duckdb"
            ) from exc

        builder = (
            SparkSession.builder.appName(self.cfg.get("app_name", "reble"))
            .master(self.cfg.get("master", "local[*]"))
            .config(
                "spark.sql.extensions",
                "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
            )
            # Loopback binding keeps local[​*] working on machines where the
            # hostname doesn't resolve to a bindable address (VPNs, sandboxes).
            .config("spark.driver.bindAddress", "127.0.0.1")
            .config("spark.driver.host", "localhost")
            .config("spark.ui.enabled", "false")
        )
        settings = dict(self.cfg.get("settings") or {})
        packages = settings.pop(
            "packages", self._default_packages()
        )
        if packages:
            builder = builder.config("spark.jars.packages", packages)
        builder = self._configure_catalog(builder, settings)
        for key, value in settings.items():
            builder = builder.config(key, value)

        with warnings.catch_warnings():
            # JVM banner noise on session start is not actionable for users.
            warnings.simplefilter("ignore")
            self._session = builder.getOrCreate()
        return self._session

    def _default_packages(self) -> str:
        packages = ICEBERG_SPARK_RUNTIME
        if self.catalog_cfg.type == "glue":
            packages += f",{GLUE_JARS}"
        if self.catalog_cfg.type == "sql":
            # Spark needs a JDBC driver for the catalog's backing database;
            # pyiceberg reaches it over SQLAlchemy instead.
            uri = str((self.catalog_cfg.model_extra or {}).get("uri", ""))
            if uri.startswith(("postgresql://", "postgresql+")):
                packages += ",org.postgresql:postgresql:42.7.4"
            else:
                packages += ",org.xerial:sqlite-jdbc:3.46.1.3"
        return packages

    def _configure_catalog(self, builder, settings: dict):
        cat = self.catalog_cfg.type
        if cat not in CATALOG_TYPES:
            raise ConfigError(
                f"catalog type '{cat}' has no Spark equivalent — Spark engine "
                "supports glue, hive, rest/polaris/nessie, and sql"
            )
        extra = {
            k: v
            for k, v in (self.catalog_cfg.model_dump(exclude={"type"}).items())
            if v is not None
        }
        prefix = f"spark.sql.catalog.{self.catalog_name}"
        # The SparkCatalog wrapper (not direct catalog-impl) is the form
        # Iceberg documents for Spark; it resolves the real catalog from
        # `type` + properties.
        builder = builder.config(
            f"{prefix}", "org.apache.iceberg.spark.SparkCatalog"
        ).config(f"{prefix}.type", CATALOG_TYPES[cat])
        # Refs move under Spark (pyiceberg promotes/branches between Spark
        # statements); a cached catalog entry would serve stale snapshots.
        builder = builder.config(f"{prefix}.cache-enabled", "false")
        if "warehouse" in extra:
            builder = builder.config(f"{prefix}.warehouse", str(extra["warehouse"]))
        if cat == "glue":
            region = extra.get("region") or extra.get("glue.region")
            if region:
                builder = builder.config(f"{prefix}.glue.region", str(region))
        elif cat in ("rest", "polaris", "nessie", "reble"):
            if "uri" not in extra:
                raise ConfigError(f"Catalog type '{cat}' requires a uri")
            builder = builder.config(f"{prefix}.uri", str(extra["uri"]))
        elif cat == "sql":
            builder = builder.config(f"{prefix}.uri", _jdbc_uri(str(extra.get("uri", ""))))
        return builder

    # ----------------------------------------------------------- snapshot io

    def qualified(self, table_id: str) -> str:
        """Table id prefixed with the Spark catalog name when needed.

        Spark resolves unqualified/bare-namespace ids against its session
        catalog, not ours — every Iceberg table Reble touches must carry the
        catalog prefix to share refs with pyiceberg.
        """
        parts = table_id.split(".")
        if len(parts) >= 3:
            return table_id
        return f"{self.catalog_name}.{table_id}"

    def register_snapshot(self, view: str, table_id: str, snapshot_id: int | None) -> None:
        """Temp view over the table pinned to a snapshot id."""
        spark = self.connect()
        target = f"{self.qualified(table_id)} VERSION AS OF {int(snapshot_id)}" if snapshot_id is not None else self.qualified(table_id)
        spark.sql(f"CREATE OR REPLACE TEMP VIEW `{view}` AS SELECT * FROM {target}")


def _jdbc_uri(sqlalchemy_uri: str) -> str:
    """pyiceberg SQLAlchemy uri → JDBC uri (sql catalogs share their database).

    sqlite:////abs/path/state.db → jdbc:sqlite:/abs/path/state.db
    postgresql://user:pass@host:port/db → jdbc:postgresql://host:port/db?user=…&password=…
    """
    if sqlalchemy_uri.startswith("sqlite:///"):
        path = sqlalchemy_uri[len("sqlite:///"):]
        return f"jdbc:sqlite:{path}"
    if sqlalchemy_uri.startswith("jdbc:"):
        return sqlalchemy_uri
    if sqlalchemy_uri.startswith(("postgresql://", "postgresql+")):
        # sqlite holds a database-level lock that Spark's JDBC pool and
        # pyiceberg would fight over — sql catalogs + Spark need a server
        # database. Postgres WAL allows both writers to interleave safely.
        from urllib.parse import urlencode, urlsplit

        parts = urlsplit(sqlalchemy_uri)
        params = {"user": parts.username}
        if parts.password:
            params["password"] = parts.password
        return f"jdbc:postgresql://{parts.hostname}:{parts.port or 5432}{parts.path}?{urlencode(params)}"
    raise ConfigError(
        f"Spark engine supports sql catalogs over sqlite (single-writer local "
        f"use) and postgresql; got '{sqlalchemy_uri}' — use a rest/glue/hive "
        "catalog for shared Spark state"
    )
