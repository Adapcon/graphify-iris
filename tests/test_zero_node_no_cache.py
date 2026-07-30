"""#1666 — an extractable source file that yields zero nodes must not be cached,
and must be surfaced.

Every supported file produces at least a file node, so a zero-node result is
anomalous (a transient batch/parallel hiccup). Caching it made the empty
byte-stable across runs and silently blinded affected/explain to the file. We
now skip the cache write for a zero-node result (so a rerun self-heals) and warn.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import graphify.extract as ex


def test_zero_node_result_not_cached_then_self_heals(tmp_path, capsys, monkeypatch):
    f = tmp_path / "thing.rb"
    f.write_text("class Foo\n  def bar; end\nend\n")

    real = ex._safe_extract_with_xaml_root

    def _empty(extractor, path, root):
        return {"nodes": [], "edges": []}

    # First run: force a zero-node extraction for this file.
    monkeypatch.setattr(ex, "_safe_extract_with_xaml_root", _empty)
    ex.extract([f], cache_root=tmp_path / "out", parallel=False)

    err = capsys.readouterr().err
    assert "zero nodes" in err and "thing.rb" in err, err

    # Second run with the real extractor: because the empty was NOT cached, the
    # file re-extracts and lands in the graph (self-heal).
    monkeypatch.setattr(ex, "_safe_extract_with_xaml_root", real)
    r2 = ex.extract([f], cache_root=tmp_path / "out", parallel=False)
    assert any(str(n.get("source_file", "")).endswith("thing.rb") for n in r2["nodes"])


def test_normal_file_still_cached(tmp_path):
    # Guard against over-correction: a normal (non-empty) result must still cache.
    f = tmp_path / "ok.rb"
    f.write_text("class Bar\n  def baz; end\nend\n")
    r1 = ex.extract([f], cache_root=tmp_path / "out", parallel=False)
    assert r1["nodes"]
    from graphify.cache import load_cached
    assert load_cached(f, tmp_path / "out") is not None, "non-empty result should be cached"


def test_no_warning_when_all_files_produce_nodes(tmp_path, capsys):
    f = tmp_path / "fine.rb"
    f.write_text("module M\n  def self.go; end\nend\n")
    ex.extract([f], cache_root=tmp_path / "out", parallel=False)
    err = capsys.readouterr().err
    assert "zero nodes" not in err


# #2258 — a deliberate `skipped` verdict is not a zero-node anomaly.
#
# extract_json declines data JSON on purpose (#1224). That produced a result with
# no nodes and no error, which the #1666 warning read as a hiccup: it told users
# to report working-as-intended files, and its "a re-run will retry them" advice
# could never clear because the skip was also excluded from the cache.

_SWAGGER = """{
  "swagger": "2.0",
  "info": {"title": "Custom API", "version": "1.0"},
  "basePath": "/custom/v10",
  "paths": {"/thing": {"get": {"operationId": "ThingGet"}}}
}
"""


def _skipped_json(tmp_path):
    pytest.importorskip("tree_sitter_json")
    f = tmp_path / "custom.json"
    f.write_text(_SWAGGER, encoding="utf-8")
    from graphify.extractors.json_config import extract_json
    assert extract_json(f).get("skipped"), "fixture must be a skipped data json"
    return f


def test_skipped_data_json_does_not_warn(tmp_path, capsys):
    f = _skipped_json(tmp_path)
    ex.extract([f], cache_root=tmp_path / "out", parallel=False)
    err = capsys.readouterr().err
    assert "zero nodes" not in err, err
    assert "#1666" not in err, err


def test_skipped_data_json_is_cached(tmp_path):
    # The verdict is stable, so it must not be re-parsed on every run.
    f = _skipped_json(tmp_path)
    ex.extract([f], cache_root=tmp_path / "out", parallel=False)
    from graphify.cache import load_cached
    cached = load_cached(f, tmp_path / "out")
    assert cached is not None, "skipped verdict should be cached"
    assert not cached.get("nodes")
