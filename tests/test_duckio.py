"""duckio: S3 auto-configuration via boto3 (stubbed — no real creds)."""

from __future__ import annotations

from types import SimpleNamespace

from reble import duckio


def _stub_boto(monkeypatch, region="us-east-1", token=None):
    """Stub boto3 resolution inside duckio; capture what gets SET."""
    creds = SimpleNamespace(
        access_key="AKIATEST", secret_key="secret", token=token
    )
    monkeypatch.setattr(
        duckio, "_boto3_s3_credentials", lambda: (region, creds)
    )
    duckio._S3_CRED_CACHE.clear()
    return creds


def test_s3_auto_config_sets_region_and_creds(monkeypatch):
    _stub_boto(monkeypatch)
    io = duckio.DuckIo({"read_mode": "arrow"})  # arrow: no iceberg load
    con = io.connect()
    assert con.execute("SELECT current_setting('s3_region')").fetchone()[0] == "us-east-1"
    assert con.execute("SELECT current_setting('s3_access_key_id')").fetchone()[0] == "AKIATEST"
    assert con.execute("SELECT current_setting('s3_secret_access_key')").fetchone()[0] == "secret"
    con.close()


def test_s3_auto_config_session_token_when_present(monkeypatch):
    _stub_boto(monkeypatch, region="eu-west-1", token="tok123")
    io = duckio.DuckIo({"read_mode": "arrow"})
    con = io.connect()
    assert con.execute("SELECT current_setting('s3_region')").fetchone()[0] == "eu-west-1"
    assert con.execute("SELECT current_setting('s3_session_token')").fetchone()[0] == "tok123"
    con.close()


def test_manual_settings_beat_auto_config(monkeypatch):
    _stub_boto(monkeypatch)
    io = duckio.DuckIo(
        {"read_mode": "arrow", "settings": {"s3_region": "ap-south-1"}}
    )
    con = io.connect()
    assert con.execute("SELECT current_setting('s3_region')").fetchone()[0] == "ap-south-1"
    con.close()


def test_no_credentials_is_silent(monkeypatch):
    monkeypatch.setattr(duckio, "_boto3_s3_credentials", lambda: None)
    duckio._S3_CRED_CACHE.clear()
    io = duckio.DuckIo({"read_mode": "arrow"})
    con = io.connect()  # must not raise on local-only setups
    con.close()
