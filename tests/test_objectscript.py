"""Tests for the InterSystems IRIS ObjectScript extractors and resolver.

Three layers are covered:

* per-file extraction of `.mac` routines, `.cls` classes and `.inc` macro
  includes (`graphify.extractors.objectscript`);
* suffix dispatch, which must reroute `.cls` away from Apex and `.inc` away from
  Pascal ONLY for files carrying an ObjectScript-only marker;
* the corpus pass that binds `$$Label^ROUTINE` / `##class(Pkg.Cls).Method()` /
  `#include %MACROS` to real nodes (`graphify.objectscript_resolution`), which is
  where every cross-file ObjectScript edge is minted.
"""
from __future__ import annotations

from pathlib import Path

from graphify.extract import (
    _get_extractor,
    extract,
    extract_apex,
    extract_objectscript,
    extract_objectscript_class,
    extract_objectscript_include,
    extract_pascal,
)

FIXTURES = Path(__file__).parent / "fixtures"
ROUTINE = FIXTURES / "sampleiris.mac"
CLASS = FIXTURES / "SampleClass.cls"
INCLUDE = FIXTURES / "sampleinc.inc"

CROSS = FIXTURES / "iris_cross_file"
SCREEN = CROSS / "ORDER680.mac"
RULES = CROSS / "ORDER680RG.mac"
CROSS_CLASS = CROSS / "Sample.cls"
CROSS_LINE = CROSS / "SampleLine.cls"
CROSS_HELPER = CROSS / "Helper.cls"
CROSS_INCLUDE = CROSS / "_ERPMACROS.inc"


def _labels(result: dict) -> list[str]:
    return [n["label"] for n in result["nodes"]]


def _relations(result: dict) -> set[str]:
    return {e["relation"] for e in result["edges"]}


def _edge_labels(result: dict, relation: str) -> set[tuple[str, str]]:
    by_id = {n["id"]: n["label"] for n in result["nodes"]}
    return {
        (by_id.get(e["source"], e["source"]), by_id.get(e["target"], e["target"]))
        for e in result["edges"] if e["relation"] == relation
    }


def _edge(result: dict, relation: str, src_label: str, tgt_label: str) -> dict | None:
    by_id = {n["id"]: n["label"] for n in result["nodes"]}
    for e in result["edges"]:
        if e["relation"] != relation:
            continue
        if by_id.get(e["source"]) == src_label and by_id.get(e["target"]) == tgt_label:
            return e
    return None


def _raw(result: dict, **match) -> list[dict]:
    return [
        rc for rc in result.get("raw_calls", [])
        if all(rc.get(k) == v for k, v in match.items())
    ]


def _contexts(result: dict, relation: str, tgt_label: str) -> set[str]:
    by_id = {n["id"]: n["label"] for n in result["nodes"]}
    return {
        str(e.get("context", "")) for e in result["edges"]
        if e["relation"] == relation and by_id.get(e["target"]) == tgt_label
    }


# ── routines (.mac) ──────────────────────────────────────────────────────────


def test_routine_file_node():
    r = extract_objectscript(ROUTINE)
    assert "sampleiris.mac" in _labels(r)
    assert "error" not in r


def test_routine_labels_become_nodes():
    labels = _labels(extract_objectscript(ROUTINE))
    for name in ("GetOrder()", "ValidateOrder()", "SaveOrder()"):
        assert name in labels
    # A numeric / mixed label is a real entry point in screen-driven code.
    assert "9000()" in labels
    assert "9100SN()" in labels


def test_routine_header_is_not_a_label():
    # `ROUTINE SAMPLEIRIS` is the routine header, not a label declaration.
    assert "ROUTINE()" not in _labels(extract_objectscript(ROUTINE))


def test_routine_labels_are_defined_by_the_file():
    r = extract_objectscript(ROUTINE)
    assert ("sampleiris.mac", "GetOrder()") in _edge_labels(r, "defines")


def test_same_routine_extrinsic_call_resolves_locally():
    r = extract_objectscript(ROUTINE)
    assert _edge(r, "calls", "GetOrder()", "ValidateOrder()") is not None


def test_goto_resolves_to_local_label():
    r = extract_objectscript(ROUTINE)
    assert _edge(r, "calls", "9100SN()", "9000()") is not None


def test_macro_invocation_is_not_a_call():
    # `$$$VAR` / `$$$OK` are macros, not extrinsic functions.
    r = extract_objectscript(ROUTINE)
    assert not [t for _s, t in _edge_labels(r, "calls") if t.startswith("VAR")]
    assert not _raw(r, label="VAR")
    assert not _raw(r, label="OK")


def test_global_read_and_write_contexts():
    r = extract_objectscript(ROUTINE)
    assert "^SAMPLEORDER" in _labels(r)
    contexts = _contexts(r, "references", "^SAMPLEORDER")
    assert contexts == {"global_read", "global_write"}
    # `kill ^SAMPLETMP(company)` is a write even with no `=` in sight.
    assert _contexts(r, "references", "^SAMPLETMP") == {"global_write"}


def test_global_node_is_sourceless_so_it_is_shared():
    r = extract_objectscript(ROUTINE)
    node = next(n for n in r["nodes"] if n["label"] == "^SAMPLEORDER")
    assert node["source_file"] == ""


def test_cross_routine_calls_are_deferred_to_the_resolver():
    r = extract_objectscript(ROUTINE)
    # No `calls` edge may point outside this file...
    assert not _raw(r, kind="routine", routine="")
    # ...but every cross-routine reference is recorded.
    extrinsic = _raw(r, kind="routine", routine="SAMPLEIRISRG", label="WriteLog")
    assert extrinsic and extrinsic[0]["context"] == "extrinsic_function"
    assert extrinsic[0]["confidence"] == "EXTRACTED"
    assert _raw(r, kind="routine", routine="SAMPLEIRISRG", label="Audit")
    assert _raw(r, kind="routine", routine="%ERPUI", label="ShowError")
    # `do ^%ERPLOOKUP(...)` — whole routine, no entry label.
    assert _raw(r, kind="routine", routine="%ERPLOOKUP", label="")


def test_qualified_call_is_not_also_counted_as_a_global():
    r = extract_objectscript(ROUTINE)
    assert "^%ERPLOOKUP" not in _labels(r)
    assert "^SAMPLEIRISRG" not in _labels(r)


def test_class_method_call_from_a_routine_is_deferred():
    r = extract_objectscript(ROUTINE)
    assert _raw(r, kind="class", cls="Erp.SampleClass", member="save")
    assert _raw(r, kind="class", cls="Erp.SampleClass", member="%New")


def test_include_is_deferred():
    r = extract_objectscript(ROUTINE)
    inc = _raw(r, kind="include", include="%ERPMACROS")
    assert inc and inc[0]["relation"] == "imports"


def test_callback_string_is_an_inferred_call():
    r = extract_objectscript(ROUTINE)
    cb = _raw(r, kind="routine", routine="SAMPLEIRIS", label="9100SN")
    assert cb and cb[0]["confidence"] == "INFERRED"
    assert cb[0]["context"] == "callback_string"


def test_bare_caret_string_is_a_global_reference_not_a_call():
    # `set glb="^SAMPLELOG"` names a global for indirect use. Read as a callback it
    # would invent a call to whatever routine shares the name (in the real ERP,
    # `"^%APPLOG"` the global vs `_APPLOG.mac` the routine).
    r = extract_objectscript(ROUTINE)
    assert not _raw(r, kind="routine", routine="SAMPLELOG")
    assert "^SAMPLELOG" in _labels(r)
    edge = _edge(r, "references", "SaveOrder()", "^SAMPLELOG")
    assert edge is not None
    assert edge["confidence"] == "INFERRED"
    assert edge["context"] == "global_name_string"
    # It is not counted as a read or a write: this line is neither.
    assert _contexts(r, "references", "^SAMPLELOG") == {"global_name_string"}


def test_comment_text_produces_nothing():
    r = extract_objectscript(ROUTINE)
    # "Confirmation callback" and friends live after `;` and must be invisible.
    assert not [lbl for lbl in _labels(r) if " " in lbl]


def test_routine_missing_file_returns_empty():
    r = extract_objectscript(Path("nonexistent.mac"))
    assert r["nodes"] == []
    assert r["edges"] == []


def test_routine_has_no_dangling_edges():
    r = extract_objectscript(ROUTINE)
    ids = {n["id"] for n in r["nodes"]}
    for e in r["edges"]:
        assert e["source"] in ids and e["target"] in ids, e


# ── classes (.cls) ───────────────────────────────────────────────────────────


def test_class_node_uses_the_declared_dotted_name():
    r = extract_objectscript_class(CLASS)
    assert "Erp.SampleClass" in _labels(r)
    assert ("SampleClass.cls", "Erp.SampleClass") in _edge_labels(r, "contains")


def test_class_methods_and_properties():
    r = extract_objectscript_class(CLASS)
    labels = _labels(r)
    for name in (".save()", ".validate()", ".writeLog()"):
        assert name in labels
    for name in (".code", ".status", ".items"):
        assert name in labels
    assert ("Erp.SampleClass", ".save()") in _edge_labels(r, "method")
    assert ("Erp.SampleClass", ".code") in _edge_labels(r, "defines")


def test_declaration_metadata_is_not_a_node():
    labels = _labels(extract_objectscript_class(CLASS))
    # Parameter / Index / Storage carry no behavior and no data of their own.
    assert ".AUTHOR" not in labels and ".AUTHOR()" not in labels
    assert ".rowId" not in labels
    assert ".SampleStorage()" not in labels


def test_self_method_call_resolves_in_file():
    r = extract_objectscript_class(CLASS)
    assert _edge(r, "calls", ".validate()", ".writeLog()") is not None


def test_self_property_use_is_a_reference_not_a_call():
    r = extract_objectscript_class(CLASS)
    assert _edge(r, "references", ".validate()", ".status") is not None
    assert _edge(r, "calls", ".validate()", ".status") is None


def test_extends_bases_are_deferred_with_inherits():
    r = extract_objectscript_class(CLASS)
    assert _raw(r, kind="class", relation="inherits", cls="Erp.SampleBase")
    # The `%` base is deferred too; suppressing IRIS library bases is the
    # resolver's job, so an in-corpus `%` class can still resolve.
    assert _raw(r, kind="class", relation="inherits", cls="%Library.Persistent")


def test_user_member_types_are_referenced_and_system_types_are_not():
    r = extract_objectscript_class(CLASS)
    assert _raw(r, kind="class", relation="references", cls="Erp.YesNo")
    assert _raw(r, kind="class", relation="references", cls="Erp.SampleItem")
    # `As %Library.Integer` / `As %Library.String` are datatype annotations.
    assert not [rc for rc in r["raw_calls"] if str(rc.get("cls", "")).startswith("%Library.")
                and rc.get("relation") == "references"]


def test_collection_property_references_its_element_class():
    # `As list Of SampleLine`: `list` is IRIS syntax, so the reference is the
    # element class. Capturing `list` would both lose SampleLine and mint one
    # `list` node every collection property in a corpus points at.
    r = extract_objectscript_class(CLASS)
    assert _raw(r, kind="class", relation="references", cls="SampleLine")
    assert not _raw(r, kind="class", cls="list")
    assert not _raw(r, kind="class", cls="array")


def test_class_references_carry_the_package_search_path():
    # An unqualified name resolves against the class's own package first, then its
    # `Import` packages — the resolver cannot know either without this stamp.
    r = extract_objectscript_class(CLASS)
    rc = _raw(r, kind="class", relation="references", cls="SampleLine")[0]
    assert rc["pkgs"] == "Erp,Erp.Util"
    # A routine has no package context, so nothing is stamped there.
    routine = extract_objectscript(ROUTINE)
    assert not [c for c in routine["raw_calls"] if "pkgs" in c]


def test_method_body_calls_are_attributed_to_the_method():
    r = extract_objectscript_class(CLASS)
    rc = _raw(r, kind="routine", routine="SAMPLEIRISRG", label="Audit")
    assert rc
    save_nid = next(n["id"] for n in r["nodes"] if n["label"] == ".save()")
    assert rc[0]["caller_nid"] == save_nid


def test_storage_globals_are_referenced_by_the_class():
    r = extract_objectscript_class(CLASS)
    assert _edge(r, "references", "Erp.SampleClass", "^SAMPLEDATA") is not None


def test_global_written_inside_a_method():
    r = extract_objectscript_class(CLASS)
    assert _edge(r, "references", ".validate()", "^SAMPLELOG") is not None
    assert "global_write" in _contexts(r, "references", "^SAMPLELOG")


def test_class_has_no_dangling_edges():
    r = extract_objectscript_class(CLASS)
    ids = {n["id"] for n in r["nodes"]}
    for e in r["edges"]:
        assert e["source"] in ids and e["target"] in ids, e


def test_class_file_without_a_class_declaration_yields_only_the_file(tmp_path):
    source = tmp_path / "Empty.cls"
    source.write_text("/// truncated export\nClassMethod orphan()\n", encoding="utf-8")
    r = extract_objectscript_class(source)
    assert _labels(r) == ["Empty.cls"]
    assert r["edges"] == []


# ── macro includes (.inc) ────────────────────────────────────────────────────


def test_include_macros_become_nodes():
    r = extract_objectscript_include(INCLUDE)
    labels = _labels(r)
    assert "sampleinc.inc" in labels
    assert "$$$VAR" in labels
    assert "$$$OK" in labels
    assert "$$$SetG" in labels
    assert ("sampleinc.inc", "$$$VAR") in _edge_labels(r, "defines")


def test_nested_include_is_deferred():
    r = extract_objectscript_include(INCLUDE)
    assert _raw(r, kind="include", include="%ERPOUT")


def test_macro_bodies_are_not_scanned_for_globals():
    # `#define VAR ... set z="^" ...` must not mint a global node.
    r = extract_objectscript_include(INCLUDE)
    assert not [lbl for lbl in _labels(r) if lbl.startswith("^")]


# ── suffix dispatch ──────────────────────────────────────────────────────────


def test_mac_dispatches_to_objectscript():
    assert _get_extractor(ROUTINE) is extract_objectscript


def test_mac_is_collected_as_code():
    from graphify.detect import CODE_EXTENSIONS
    assert ".mac" in CODE_EXTENSIONS


def test_iris_cls_dispatches_to_objectscript():
    assert _get_extractor(CLASS) is extract_objectscript_class


def test_apex_cls_still_dispatches_to_apex():
    assert _get_extractor(FIXTURES / "sample.cls") is extract_apex


def test_iris_inc_dispatches_to_objectscript():
    assert _get_extractor(INCLUDE) is extract_objectscript_include


def test_bom_does_not_hide_the_first_declaration(tmp_path):
    # Every ObjectScript declaration is anchored at column 0, so a BOM in front of
    # one hides it: the class would be routed to Apex and the routine would lose its
    # first label.
    cls = tmp_path / "Bom.cls"
    mac = tmp_path / "BOMROU.mac"
    cls.write_text(
        "Class Erp.Bom Extends %Library.Persistent\n{\nProperty code As %Library.Integer;\n}\n",
        encoding="utf-8-sig",
    )
    mac.write_text("BOMROU\t; header label\n\tquit\n", encoding="utf-8-sig")

    assert _get_extractor(cls) is extract_objectscript_class
    assert "Erp.Bom" in _labels(extract_objectscript_class(cls))
    assert "BOMROU()" in _labels(extract_objectscript(mac))


def test_c_style_inc_does_not_dispatch_to_objectscript(tmp_path):
    # A C/C++ `.inc` carries the same `#define`/`#include` directives as an IRIS
    # macro include, so the sniff must not claim it: it names its include target as
    # a path, which ObjectScript never does.
    source = tmp_path / "vectors.inc"
    source.write_text(
        '#include <stdio.h>\n#include "vec.h"\n#define VEC_MAX 16\n', encoding="utf-8"
    )
    assert _get_extractor(source) is extract_pascal


def test_pascal_inc_still_dispatches_to_pascal(tmp_path):
    source = tmp_path / "helpers.inc"
    source.write_text(
        "{$IFDEF DEBUG}\nprocedure Log(const S: string);\nbegin\nend;\n{$ENDIF}\n",
        encoding="utf-8",
    )
    assert _get_extractor(source) is extract_pascal


# ── cross-file resolution ────────────────────────────────────────────────────


def test_resolver_is_registered():
    from graphify.resolver_registry import registered_resolvers
    assert "objectscript_references" in {r.name for r in registered_resolvers()}


def _corpus(tmp_path) -> dict:
    return extract(
        [SCREEN, RULES, CROSS_CLASS, CROSS_LINE, CROSS_HELPER, CROSS_INCLUDE],
        cache_root=tmp_path,
        parallel=False,
    )


def test_extrinsic_call_binds_to_the_label_in_the_other_routine(tmp_path):
    graph = _corpus(tmp_path)
    edge = _edge(graph, "calls", "0000()", "Build()")
    assert edge is not None
    assert edge["confidence"] == "EXTRACTED"
    by_id = {n["id"]: n for n in graph["nodes"]}
    assert by_id[edge["target"]]["source_file"].endswith("ORDER680RG.mac")


def test_do_label_routine_binds_to_the_label(tmp_path):
    graph = _corpus(tmp_path)
    assert _edge(graph, "calls", "0000()", "Initialize()") is not None


def test_class_method_call_binds_to_the_method_node(tmp_path):
    graph = _corpus(tmp_path)
    assert _edge(graph, "calls", "0000()", ".save()") is not None


def test_include_binds_to_the_percent_include_file(tmp_path):
    # `#include %ERPMACROS` must find `_ERPMACROS.inc` (`%` is exported as `_`).
    graph = _corpus(tmp_path)
    assert _edge(graph, "imports", "ORDER680.mac", "_ERPMACROS.inc") is not None


def test_callback_string_binds_back_to_its_own_routine(tmp_path):
    graph = _corpus(tmp_path)
    edge = _edge(graph, "calls", "0000()", "9100SN()")
    assert edge is not None
    assert edge["confidence"] == "INFERRED"


def test_unqualified_class_resolves_through_its_own_package(tmp_path):
    # `Property lines As list Of SampleLine` inside `Erp.Sample` is `Erp.SampleLine`.
    graph = _corpus(tmp_path)
    assert _edge(graph, "references", "Erp.Sample", "Erp.SampleLine") is not None
    assert not [n for n in graph["nodes"] if n["label"] in ("SampleLine", "list")]


def test_unqualified_class_resolves_through_an_import(tmp_path):
    # `##class(Helper).run()` inside a file that declares `Import Erp.Util`.
    graph = _corpus(tmp_path)
    assert _edge(graph, "calls", ".save()", ".run()") is not None
    assert not [n for n in graph["nodes"] if n["label"] == "Helper"]


def test_unresolved_routine_becomes_one_shared_external_node(tmp_path):
    graph = _corpus(tmp_path)
    externals = [n for n in graph["nodes"] if n["label"] == "%ERPUI"]
    assert len(externals) == 1, "a routine outside the corpus gets exactly one node"
    assert externals[0]["id"].startswith("ref")
    assert externals[0]["source_file"] == ""
    assert _edge(graph, "calls", "0000()", "%ERPUI") is not None


def test_unresolved_iris_library_class_is_suppressed(tmp_path):
    # `##class(%Library.File)` and `Extends %Library.Persistent` are standard
    # library: no external node, so they cannot become god nodes.
    graph = _corpus(tmp_path)
    assert not [n for n in graph["nodes"] if str(n["label"]).startswith("%Library.")]


def test_resolution_has_no_dangling_edges(tmp_path):
    graph = _corpus(tmp_path)
    ids = {n["id"] for n in graph["nodes"]}
    for e in graph["edges"]:
        assert e["source"] in ids and e["target"] in ids, e


def test_routine_labels_do_not_bind_calls_from_another_language(tmp_path):
    # ERP label names (`Build`, `Set`, `New`, `0000`) collide with ordinary names in
    # every other language, so a Python call to its own `Build` must not resolve to
    # an IRIS label of that name just because the corpus contains both.
    py = tmp_path / "app.py"
    mac = tmp_path / "RULES.mac"
    py.write_text(
        "from helpers import Build\n\ndef run():\n    return Build(1)\n", encoding="utf-8"
    )
    mac.write_text("ROUTINE RULES\nRULES\t;\nBuild(x)\t;\n\tquit 1\n", encoding="utf-8")

    graph = extract([py, mac], cache_root=tmp_path / "out", root=tmp_path, parallel=False)
    mac_nodes = {n["id"] for n in graph["nodes"] if str(n.get("source_file", "")).endswith(".mac")}
    from_py = [
        e for e in graph["edges"]
        if e["target"] in mac_nodes and str(e.get("source_file", "")).endswith(".py")
    ]
    assert from_py == []


def test_global_is_not_absorbed_by_a_same_named_routine_label(tmp_path):
    # A routine's header label conventionally repeats the routine name, and a
    # routine commonly maintains the same-named global — so `^TARGET` (a data node,
    # deliberately sourceless because it is shared) and the label `TARGET()` collide
    # on the stub-rewire pass's label key. The global must stay its own node instead
    # of being merged into the label, which would delete it and turn every read and
    # write of it into a call into code.
    target = tmp_path / "TARGET.mac"
    caller = tmp_path / "CALLER.mac"
    target.write_text("ROUTINE TARGET\nTARGET\t; header\n\tquit\n", encoding="utf-8")
    caller.write_text(
        "ROUTINE CALLER\nCALLER\t;\nMain\t;\n\tset x=^TARGET(1)\n\tquit\n", encoding="utf-8"
    )

    graph = extract([target, caller], cache_root=tmp_path / "out", root=tmp_path, parallel=False)
    glob = [n for n in graph["nodes"] if n["label"] == "^TARGET"]
    assert len(glob) == 1
    assert _edge(graph, "references", "Main()", "^TARGET") is not None
    assert _edge(graph, "references", "Main()", "TARGET()") is None


def test_duplicated_routine_name_binds_to_the_nearest_tree(tmp_path):
    # One routine name, two trees: an ERP workspace holds the same routine once per
    # product version and again under each customer's customization. A caller in the
    # customization tree means ITS copy, so the shared-prefix winner is the edge and
    # the far copy gets none.
    caller = tmp_path / "custom" / "acme" / "routines" / "CUST" / "CALLER.mac"
    near = tmp_path / "custom" / "acme" / "routines" / "CUST" / "TARGET.mac"
    far = tmp_path / "v7.5" / "erp" / "routines" / "SALES" / "TARGET.mac"
    for path, text in (
        (caller, "ROUTINE CALLER\nCALLER\t;\nMain\t;\n\tset sc=$$Run^TARGET(1)\n\tquit\n"),
        (near, "ROUTINE TARGET\nTARGET\t;\nRun(x)\t;\n\tquit 1\n"),
        (far, "ROUTINE TARGET\nTARGET\t;\nRun(x)\t;\n\tquit 1\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    graph = extract([caller, near, far], cache_root=tmp_path / "out", root=tmp_path, parallel=False)
    by_id = {n["id"]: n for n in graph["nodes"]}
    bound = [
        by_id[e["target"]]["source_file"] for e in graph["edges"]
        if e["relation"] == "calls" and by_id.get(e["source"], {}).get("label") == "Main()"
    ]
    assert len(bound) == 1, bound
    assert "custom/acme" in bound[0].replace("\\", "/")


def test_ambiguous_routine_name_emits_no_call_edge(tmp_path):
    # The same routine name in two unrelated trees (a multi-version scan) is not
    # resolvable: equally-distant candidates must produce no edge, not a guess.
    caller = tmp_path / "custom" / "CALLER.mac"
    v1 = tmp_path / "v1" / "TARGET.mac"
    v2 = tmp_path / "v2" / "TARGET.mac"
    for path, text in (
        (caller, "ROUTINE CALLER\nCALLER\t;\nMain\t;\n\tdo Run^TARGET(1)\n\tquit\n"),
        (v1, "ROUTINE TARGET\nTARGET\t;\nRun(x)\t;\n\tquit 1\n"),
        (v2, "ROUTINE TARGET\nTARGET\t;\nRun(x)\t;\n\tquit 1\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    graph = extract([caller, v1, v2], cache_root=tmp_path / "out", parallel=False)
    assert _edge(graph, "calls", "Main()", "Run()") is None
    # Nor is a phantom external minted for a name the corpus does contain.
    assert not [n for n in graph["nodes"] if n["label"] == "TARGET"]
