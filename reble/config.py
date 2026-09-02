"""reble.yml schema, ${VAR} interpolation, and precedence. Normative: spec section 3.

Precedence: CLI flag → env var (REBLE_*) → profile → reble.yml → built-in default.
Secrets come from environment interpolation only; init refuses to save them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import ConfigError

VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

SECRET_HINTS = ("secret", "password", "token", "credential", "api_key", "access_key")


class CatalogConfig(BaseModel):
    type: Literal[
        "glue", "polaris", "nessie", "hive", "rest", "reble",
        "sql", "in-memory", "dynamodb", "bigquery",
    ]
    model_config = ConfigDict(extra="allow")  # type-specific keys (uri, warehouse, ...)


class WarehouseConfig(BaseModel):
    catalog: CatalogConfig
    default_base: str = "main"
    namespace: str | None = None  # Iceberg namespace for model tables


class LineageConfig(BaseModel):
    models_path: str = "models"  # models/**/*.sql, one file = one model
    dialect: str = "duckdb"  # SQLGlot dialect: AST hashing + lineage parsing


class BranchingConfig(BaseModel):
    git_sync: bool = True
    name_sanitization: dict[str, str] = Field(default_factory=lambda: {"/": "__", " ": "_"})
    pin_inputs: bool = True
    tag_prefix: str = "reble_pin__"
    ttl_days: int = 14


class DiffConfig(BaseModel):
    keys: dict[str, list[str]] = Field(default_factory=dict)
    on_missing_key: Literal["hash", "error"] = "hash"
    max_rows_dumped: int = 1000


class ComputePolicy(BaseModel):
    prefer: Literal["duckdb", "spark"] = "duckdb"


class EnginesConfig(BaseModel):
    duckdb: dict[str, Any] = Field(default_factory=dict)
    spark: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="allow")


class ProfileConfig(BaseModel):
    compute_policy: ComputePolicy | None = None


class Config(BaseModel):
    version: int = 1
    warehouse: WarehouseConfig
    lineage: LineageConfig = Field(default_factory=LineageConfig)
    branching: BranchingConfig = Field(default_factory=BranchingConfig)
    diff: DiffConfig = Field(default_factory=DiffConfig)
    engines: EnginesConfig = Field(default_factory=EnginesConfig)
    compute_policy: ComputePolicy = Field(default_factory=ComputePolicy)
    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)


def interpolate(raw: Any, environ: dict[str, str] | None = None) -> Any:
    """Replace ${VAR} from the environment. Missing vars are a config error (exit 2)."""
    env = environ if environ is not None else dict(os.environ)
    if isinstance(raw, dict):
        return {k: interpolate(v, env) for k, v in raw.items()}
    if isinstance(raw, list):
        return [interpolate(v, env) for v in raw]
    if isinstance(raw, str):
        def sub(match: re.Match[str]) -> str:
            var = match.group(1)
            if var not in env:
                raise ConfigError(f"Environment variable {var} is not set (referenced in reble.yml)")
            return env[var]

        return VAR_PATTERN.sub(sub, raw)
    return raw


def assert_no_secrets(data: dict) -> None:
    """Never write secrets to reble.yml (spec section 3)."""

    def walk(node: Any, path: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                key_path = f"{path}.{key}" if path else str(key)
                if (
                    any(h in str(key).lower() for h in SECRET_HINTS)
                    and isinstance(value, str)
                    and value
                    and not VAR_PATTERN.fullmatch(value.strip())
                ):
                    raise ConfigError(
                        f"reble.yml would contain a secret at '{key_path}'. "
                        f"Use ${{ENV_VAR}} interpolation instead."
                    )
                walk(value, key_path)
        elif isinstance(node, list):
            for item in node:
                walk(item, path)

    walk(data)


class ConfigLoader:
    """Loads config with precedence: CLI flag → REBLE_* env → profile → reble.yml → default."""

    def __init__(self, project_root: Path | None = None):
        self.project_root = (project_root or Path.cwd()).resolve()
        self.config_path = self.project_root / "reble.yml"
        self.reble_dir = self.project_root / ".reble"

    def load(self, profile: str | None = None, overrides: dict[str, Any] | None = None) -> Config:
        if not self.config_path.exists():
            raise ConfigError(
                f"No reble.yml at {self.config_path}. Run `reble init` first."
            )
        try:
            raw = yaml.safe_load(self.config_path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"reble.yml is not valid YAML: {exc}") from exc

        raw = interpolate(raw)

        profile_name = profile or os.environ.get("REBLE_PROFILE")
        if profile_name:
            if profile_name not in raw.get("profiles", {}):
                raise ConfigError(f"Profile '{profile_name}' not found in reble.yml")
            self._apply_profile(raw, profile_name)

        self._apply_env_overrides(raw)
        self._apply_overrides(raw, overrides or {})

        try:
            return Config.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(f"reble.yml is invalid:\n{exc}") from exc

    @staticmethod
    def _apply_profile(raw: dict, profile_name: str) -> None:
        profile = raw["profiles"][profile_name]
        for key, value in profile.items():
            if isinstance(value, dict) and isinstance(raw.get(key), dict):
                raw[key] = {**raw[key], **value}
            else:
                raw[key] = value

    @staticmethod
    def _apply_env_overrides(raw: dict) -> None:
        # REBLE_ + double-underscore path: REBLE_COMPUTE_POLICY__PREFER=spark
        for key, value in os.environ.items():
            if not key.startswith("REBLE_") or key == "REBLE_PROFILE":
                continue
            path = key[len("REBLE_"):].lower().split("__")
            node = raw
            for part in path[:-1]:
                node = node.setdefault(part, {})
                if not isinstance(node, dict):
                    break
            else:
                try:
                    parsed = yaml.safe_load(value)
                except yaml.YAMLError:
                    parsed = value
                node[path[-1]] = parsed

    @staticmethod
    def _apply_overrides(raw: dict, overrides: dict[str, Any]) -> None:
        for dotted, value in overrides.items():
            parts = dotted.split(".")
            node = raw
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
