"""reble.yml: precedence, interpolation, secrets, defaults."""

from __future__ import annotations

import pytest

from reble.config import (
    Config,
    ConfigLoader,
    assert_no_secrets,
    interpolate,
)
from reble.errors import ConfigError


def _write_cfg(tmp_path, text: str):
    (tmp_path / "reble.yml").write_text(text)


BASE = """
version: 1
warehouse:
  catalog: { type: sql, uri: sqlite:///%s/catalog.db }
  namespace: ns
"""


def test_defaults(tmp_path):
    _write_cfg(tmp_path, BASE % tmp_path)
    cfg = ConfigLoader(tmp_path).load()
    assert cfg.warehouse.default_base == "main"
    assert cfg.lineage.models_path == "models"
    assert cfg.lineage.dialect == "duckdb"
    assert cfg.branching.git_sync is True
    assert cfg.branching.tag_prefix == "reble_pin__"
    assert cfg.branching.ttl_days == 14
    assert cfg.diff.on_missing_key == "hash"
    assert cfg.diff.max_rows_dumped == 1000
    assert cfg.compute_policy.prefer == "duckdb"


def test_env_var_interpolation_and_missing_var(tmp_path, monkeypatch):
    monkeypatch.setenv("CAT_URI", "sqlite:///:memory:")
    monkeypatch.setenv("MISSING_NS", "ns")
    _write_cfg(
        tmp_path,
        "version: 1\nwarehouse:\n"
        "  catalog: { type: sql, uri: '${CAT_URI}' }\n"
        "  namespace: '${MISSING_NS}'\n",
    )
    cfg = ConfigLoader(tmp_path).load()
    assert cfg.warehouse.catalog.model_dump(exclude={"type"})["uri"] == "sqlite:///:memory:"
    assert cfg.warehouse.namespace == "ns"
    with pytest.raises(ConfigError) as exc:
        interpolate({"x": "${MISSING_NS}"}, environ={})
    assert exc.value.exit_code == 2


def test_env_overrides_beat_yaml(tmp_path, monkeypatch):
    _write_cfg(tmp_path, BASE % tmp_path)
    monkeypatch.setenv("REBLE_BRANCHING__TTL_DAYS", "3")
    monkeypatch.setenv("REBLE_DIFF__ON_MISSING_KEY", "error")
    cfg = ConfigLoader(tmp_path).load()
    assert cfg.branching.ttl_days == 3
    assert cfg.diff.on_missing_key == "error"


def test_profile_overrides_and_cli_beat_all(tmp_path, monkeypatch):
    _write_cfg(
        tmp_path,
        (BASE % tmp_path)
        + "compute_policy: { prefer: duckdb }\n"
        "profiles:\n  ci:\n    compute_policy: { prefer: spark }\n",
    )
    loader = ConfigLoader(tmp_path)
    assert loader.load(profile="ci").compute_policy.prefer == "spark"
    assert loader.load(overrides={"compute_policy.prefer": "duckdb"}).compute_policy.prefer == "duckdb"
    with pytest.raises(ConfigError, match="not found"):
        loader.load(profile="nope")


def test_secret_keys_refused(tmp_path):
    with pytest.raises(ConfigError, match="secret"):
        assert_no_secrets({"warehouse": {"catalog": {"type": "rest", "token": "abc123"}}})
    # ${VAR} placeholders are fine
    assert_no_secrets({"warehouse": {"catalog": {"type": "rest", "credential": "${TOKEN}"}}})


def test_missing_config_is_exit_2(tmp_path):
    with pytest.raises(ConfigError) as exc:
        ConfigLoader(tmp_path).load()
    assert exc.value.exit_code == 2


def test_invalid_yaml_is_exit_2(tmp_path):
    _write_cfg(tmp_path, "version: [\nwarehouse: {")
    with pytest.raises(ConfigError):
        ConfigLoader(tmp_path).load()


def test_unknown_catalog_type_rejected(tmp_path):
    _write_cfg(tmp_path, "version: 1\nwarehouse:\n  catalog: { type: firebase }\n")
    with pytest.raises(ConfigError):
        ConfigLoader(tmp_path).load()


def test_rest_catalog_requires_uri(tmp_path):
    _write_cfg(tmp_path, "version: 1\nwarehouse:\n  catalog: { type: polaris }\n")
    cfg = Config.model_validate({"version": 1, "warehouse": {"catalog": {"type": "polaris"}}})
    from reble.catalog import load_catalog

    with pytest.raises(ConfigError) as exc:
        load_catalog(cfg.warehouse.catalog)
    assert exc.value.exit_code == 2
