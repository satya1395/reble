"""Row-level + schema diff engine."""

from __future__ import annotations

import pyarrow as pa
import pytest

from reble.diff import diff_arrow, resolve_keys
from reble.errors import MissingDiffKey


def _tbl(rows, schema=None):
    if schema is None:
        schema = pa.schema([("id", pa.int64()), ("v", pa.string())])
    return pa.Table.from_pylist(rows, schema=schema)


def test_keyed_diff_counts_and_samples():
    base = _tbl([{"id": 1, "v": "a"}, {"id": 2, "v": "b"}, {"id": 3, "v": "c"}])
    branch = _tbl([{"id": 2, "v": "B"}, {"id": 3, "v": "c"}, {"id": 4, "v": "d"}])
    d = diff_arrow("t", base, branch, keys=["id"])
    assert d.key_columns == ["id"]
    assert d.added_count == 1
    assert d.removed_count == 1
    assert d.changed_count == 1
    assert d.unchanged_count == 1
    assert d.added_samples == [{"id": 4, "v": "d"}]
    assert d.removed_samples == [{"id": 1, "v": "a"}]
    assert d.changed_samples == [{"id": 2, "v": "B"}]
    assert d.to_dict()["schema_delta"] == {"added": [], "removed": [], "changed": []}


def test_hash_fallback_with_warning():
    base = _tbl([{"id": 1, "v": "a"}])
    branch = _tbl([{"id": 1, "v": "a"}, {"id": 2, "v": "b"}])
    d = diff_arrow("t", base, branch, keys=None)
    assert d.key_columns is None
    assert d.added_count == 1
    assert d.removed_count == 0
    assert d.warning and "full-hash compare" in d.warning


def test_schema_delta():
    base = _tbl([{"id": 1, "v": "a"}])
    branch = pa.Table.from_pylist(
        [{"id": 1, "v": "a", "extra": 5.0}],
        schema=pa.schema([("id", pa.int64()), ("v", pa.string()), ("extra", pa.float64())]),
    )
    d = diff_arrow("t", base, branch, keys=["id"])
    assert d.schema_added == ["extra"]


def test_max_rows_dumped_caps_samples():
    base = _tbl([], schema=pa.schema([("id", pa.int64()), ("v", pa.string())]))
    branch = _tbl([{"id": i, "v": "x"} for i in range(50)])
    d = diff_arrow("t", base, branch, keys=["id"], max_rows_dumped=10)
    assert len(d.added_samples) == 10
    assert d.added_count == 50


def test_resolve_keys_precedence_and_error():
    assert resolve_keys("t", {"t": ["k"]}, ["inferred"], "hash") == ["k"]
    assert resolve_keys("other", {}, ["inferred"], "hash") == ["inferred"]
    with pytest.raises(MissingDiffKey) as exc:
        resolve_keys("t", {}, [], "error")
    assert exc.value.exit_code == 7
    assert resolve_keys("t", {}, [], "hash") is None
