from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from reble.errors import ProjectError

CONFIG_FILE = "reble.yml"


@dataclass
class RebleConfig:
    project_dir: Path
    warehouse: str = "warehouse"          # path or s3:// URI
    catalog_uri: str = ""                 # empty -> sqlite inside warehouse (local mode)
    default_branch_ttl_days: int = 14

    @property
    def warehouse_path(self) -> str:
        if "://" in self.warehouse:
            return self.warehouse
        return f"file://{(self.project_dir / self.warehouse).resolve()}"

    @property
    def resolved_catalog_uri(self) -> str:
        if self.catalog_uri:
            return self.catalog_uri
        wh = (self.project_dir / self.warehouse).resolve()
        return f"sqlite:///{wh}/catalog.db"

    @property
    def state_path(self) -> Path:
        return self.project_dir / ".reble" / "state.db"


def find_project_dir(start: Path | None = None) -> Path:
    """Walk up from `start` (or cwd) to the nearest directory containing reble.yml."""
    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / CONFIG_FILE).exists():
            return candidate
    raise ProjectError(
        f"not a reble project (no {CONFIG_FILE} found in {cur} or any parent); "
        "run `reble init <name>` to create one"
    )


def load_config(project_dir: Path | None = None) -> RebleConfig:
    project_dir = project_dir or find_project_dir()
    raw = yaml.safe_load((project_dir / CONFIG_FILE).read_text()) or {}
    known = {"warehouse", "catalog_uri", "default_branch_ttl_days"}
    unknown = set(raw) - known
    if unknown:
        raise ProjectError(f"unknown keys in {CONFIG_FILE}: {sorted(unknown)}")
    return RebleConfig(project_dir=project_dir, **raw)
