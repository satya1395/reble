from __future__ import annotations

from pathlib import Path

from reble.errors import ProjectError

_REBLE_YML = """\
# Reble project configuration
warehouse: warehouse          # local dir, or s3://bucket/prefix for team mode
catalog_uri: ""               # empty = sqlite catalog inside the warehouse (local mode)
                              # team mode: postgresql://... or an Iceberg REST catalog uri
default_branch_ttl_days: 14
"""

_SQLMESH_CONFIG = """\
gateways:
  local:
    connection:
      type: duckdb
      database: .reble/sqlmesh.db

default_gateway: local

model_defaults:
  dialect: duckdb
  start: 2026-01-01
"""

_EXAMPLE_MODEL = """\
MODEL (
  name demo.example,
  kind FULL
);

SELECT 1 AS id, 'hello reble' AS message
"""

_GITIGNORE = """\
.reble/
warehouse/
"""


def scaffold(target: Path) -> Path:
    if (target / "reble.yml").exists():
        raise ProjectError(f"{target} is already a reble project")
    (target / "models").mkdir(parents=True, exist_ok=True)
    (target / "warehouse").mkdir(exist_ok=True)
    (target / ".reble").mkdir(exist_ok=True)
    (target / "reble.yml").write_text(_REBLE_YML)
    (target / "config.yaml").write_text(_SQLMESH_CONFIG)
    (target / "models" / "example.sql").write_text(_EXAMPLE_MODEL)
    gi = target / ".gitignore"
    if not gi.exists():
        gi.write_text(_GITIGNORE)
    return target
