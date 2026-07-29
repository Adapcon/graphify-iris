"""Cross-file resolution for InterSystems IRIS ObjectScript references.

An ObjectScript reference names a ROUTINE, a CLASS or an INCLUDE FILE, never a
path: `$$Build^ORDER680RG(...)` says which routine, `##class(Erp.Order).save()`
says which class, `#include %ERPMACROS` says which macro file. Where that routine
lives on disk follows no rule the name encodes — in an ERP workspace the same
routine name commonly exists once per product version and again under each
customer's customization tree — and a class is addressed by its declared dotted
name rather than by its file name. So the per-file extractors in
`graphify.extractors.objectscript` resolve only same-file references and emit
everything else as `raw_calls`; this pass runs from
`graphify.resolver_registry` once the whole corpus is extracted and binds them.

Resolution is exact where the corpus allows it and coarser — never a guess —
where it does not:

1. routine present in the corpus and the label found in it -> edge to the label;
2. routine present, label absent (an entry point this extractor did not see, or
   `do ^ROUTINE` with no label at all) -> edge to the routine's file node;
3. routine name matching several files -> the one sharing the longest directory
   prefix with the caller wins; a tie emits nothing rather than picking one;
4. routine not in the corpus (a component or ERP-standard routine outside the
   scan) -> one shared `ref`-namespaced external node per name, so a dependency
   on `%ERPUTIL` is visible instead of silently dropped.

Classes and includes follow the same ladder (class -> member, else class; include
file, else external node), except that a class name is first expanded against the
referencing file's package search path (`_class_lookup_names`), because IRIS lets a
class be named unqualified from inside its own package.

Routines, classes and includes are indexed in SEPARATE keyspaces, never one
name->file map: one name is routinely all three at once. A utility component
ships `%ERPUTIL` as a routine (`_ERPUTIL.mac`, holding its labels) AND as an
include (`_ERPUTIL.inc`, holding its macros), so `do Log^%ERPUTIL` and
`#include %ERPUTIL` must land on different files.

The `ref` prefix is the repo-wide convention for an unresolved external reference
(#1638); `iris` is folded into the id as well so an IRIS routine named `utils` can
never merge with an unresolved JS `utils`.
"""
from __future__ import annotations

from pathlib import Path

from graphify.extractors.objectscript import LANG, _canonical_name
from graphify.ids import make_id

_ROUTINE_SUFFIX = ".mac"
_CLASS_SUFFIX = ".cls"
_INCLUDE_SUFFIX = ".inc"


def _objectscript_raw_calls(per_file: list[dict]) -> list[dict]:
    calls: list[dict] = []
    for result in per_file:
        if not isinstance(result, dict):
            continue
        for rc in result.get("raw_calls", []):
            if isinstance(rc, dict) and rc.get("lang") == LANG:
                calls.append(rc)
    return calls


class _Index:
    """Everything resolvable in the corpus, keyed the way a reference names it."""

    def __init__(self, all_nodes: list[dict], all_edges: list[dict]) -> None:
        # canonical routine/include name -> [(node id, source_file)]
        self.routine_files: dict[str, list[tuple[str, str]]] = {}
        self.include_files: dict[str, list[tuple[str, str]]] = {}
        # source_file -> {lowercased label -> node id}
        self.labels_by_file: dict[str, dict[str, str]] = {}
        # lowercased dotted class name -> [(node id, source_file)]
        self.class_nodes: dict[str, list[tuple[str, str]]] = {}
        # class node id -> {lowercased member name -> node id}
        self.members_by_class: dict[str, dict[str, str]] = {}

        node_by_id: dict[str, dict] = {}
        class_nids: set[str] = set()
        for node in all_nodes:
            nid = node.get("id")
            sf = str(node.get("source_file") or "")
            if not isinstance(nid, str) or not nid or not sf:
                continue
            node_by_id[nid] = node
            path = Path(sf)
            suffix = path.suffix.lower()
            label = str(node.get("label", ""))
            is_file_node = label == path.name
            if suffix == _ROUTINE_SUFFIX:
                if is_file_node:
                    self.routine_files.setdefault(_canonical_name(path.stem), []).append((nid, sf))
                elif label.endswith("()"):
                    self.labels_by_file.setdefault(sf, {}).setdefault(
                        label[:-2].casefold(), nid
                    )
            elif suffix == _INCLUDE_SUFFIX and is_file_node:
                self.include_files.setdefault(_canonical_name(path.stem), []).append((nid, sf))
            elif suffix == _CLASS_SUFFIX and not is_file_node and not label.startswith("."):
                # The one node per `.cls` whose label is neither the file name nor
                # a `.member` is the class itself, labelled with its dotted name.
                self.class_nodes.setdefault(label.casefold(), []).append((nid, sf))
                class_nids.add(nid)

        for edge in all_edges:
            if edge.get("relation") not in ("method", "defines"):
                continue
            src, tgt = edge.get("source"), edge.get("target")
            if src not in class_nids:
                continue
            member = node_by_id.get(str(tgt))
            if member is None:
                continue
            label = str(member.get("label", ""))
            if not label.startswith("."):
                continue
            name = label[1:].removesuffix("()").casefold()
            self.members_by_class.setdefault(str(src), {}).setdefault(name, str(tgt))


def _class_lookup_names(cls: str, pkgs: str) -> list[str]:
    """Names to try for a class reference, in IRIS resolution order.

    A reference is written unqualified (`##class(Item)`, `As list Of Item`) or
    partially qualified (`As Common.DateTime`) far more often than not, and IRIS
    resolves it against the referencing class's own package and its `Import`
    packages. `pkgs` is that search path as the extractor recorded it. The name as
    written comes FIRST, so a fully-qualified reference is never diverted; a
    `%`-prefixed name gets no prefixes at all, since `%ResultSet` is IRIS library
    and `Erp.Sales.%ResultSet` is not a name.
    """
    names = [cls]
    if not cls.startswith("%"):
        names += [f"{pkg.strip()}.{cls}" for pkg in pkgs.split(",") if pkg.strip()]
    return names


def _pick_nearest(candidates: list[tuple[str, str]], caller_file: str) -> str | None:
    """Choose the candidate whose directory shares the longest prefix with the caller.

    One routine name legitimately resolves to several files in a multi-version or
    multi-customer scan (`v7.5/routines/ORDER680.mac` and its `v7.6` sibling).
    Preferring the file nearest the caller mirrors how the code is actually
    deployed — a customization calls the routine in its own tree — and a tie
    returns None so an ambiguous name produces no edge instead of a coin flip.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][0]
    caller_parts = Path(caller_file).parent.parts

    def shared(sf: str) -> int:
        parts = Path(sf).parent.parts
        n = 0
        for a, b in zip(caller_parts, parts):
            if a != b:
                break
            n += 1
        return n

    scored = sorted(((shared(sf), nid) for nid, sf in candidates), key=lambda t: -t[0])
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def resolve_objectscript_references(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Bind ObjectScript cross-file references to real nodes (or to external stubs).

    Purely additive: emits only edges for `raw_calls` records the per-file pass
    left unresolved, skipping any (source, target, relation) pair the corpus
    already has.
    """
    raw_calls = _objectscript_raw_calls(per_file)
    if not raw_calls:
        return

    index = _Index(all_nodes, all_edges)
    existing: set[tuple[str, str, str]] = {
        (str(e.get("source")), str(e.get("target")), str(e.get("relation")))
        for e in all_edges
    }
    node_ids = {str(n.get("id")) for n in all_nodes}

    def external_nid(name: str) -> str:
        # `ref` is the repo convention for an unresolved external reference
        # (#1638); `iris` keeps it out of another language's `ref` namespace.
        nid = make_id("ref", "iris", name)
        if nid not in node_ids:
            node_ids.add(nid)
            all_nodes.append({
                "id": nid,
                "label": name,
                "file_type": "code",
                "source_file": "",
                "source_location": "",
            })
        return nid

    def emit(rc: dict, target: str) -> None:
        source = str(rc.get("caller_nid") or "")
        relation = str(rc.get("relation") or "calls")
        if not source or not target or source == target:
            return
        key = (source, target, relation)
        if key in existing:
            return
        existing.add(key)
        edge = {
            "source": source,
            "target": target,
            "relation": relation,
            "confidence": str(rc.get("confidence") or "EXTRACTED"),
            "source_file": str(rc.get("source_file", "")),
            "source_location": rc.get("source_location"),
            "weight": 1.0,
        }
        context = rc.get("context")
        if context:
            edge["context"] = context
        all_edges.append(edge)

    for rc in raw_calls:
        kind = rc.get("kind")
        caller_file = str(rc.get("source_file", ""))

        if kind == "routine":
            routine = str(rc.get("routine") or "")
            if not routine:
                continue
            candidates = index.routine_files.get(_canonical_name(routine), [])
            if candidates:
                file_nid = _pick_nearest(candidates, caller_file)
                if file_nid is None:
                    continue  # ambiguous routine name: no edge rather than a guess
                target_file = next(sf for nid, sf in candidates if nid == file_nid)
                label = str(rc.get("label") or "")
                label_nid = index.labels_by_file.get(target_file, {}).get(label.casefold())
                emit(rc, label_nid or file_nid)
            else:
                emit(rc, external_nid(routine))
            continue

        if kind == "class":
            cls = str(rc.get("cls") or "")
            if not cls:
                continue
            cls_candidates: list[tuple[str, str]] = []
            for name in _class_lookup_names(cls, str(rc.get("pkgs") or "")):
                cls_candidates = index.class_nodes.get(name.casefold(), [])
                if cls_candidates:
                    break
            if cls_candidates:
                class_nid = _pick_nearest(cls_candidates, caller_file)
                if class_nid is None:
                    continue
                member = str(rc.get("member") or "")
                member_nid = index.members_by_class.get(class_nid, {}).get(member.casefold())
                emit(rc, member_nid or class_nid)
            elif not cls.startswith("%"):
                emit(rc, external_nid(cls))
            # A `%`-prefixed class that is NOT in the corpus is IRIS standard
            # library (%Library.Persistent, %SYSTEM.OBJ, %File, %XML.Adaptor). Every
            # class extends one and every routine calls into them, so an external
            # node for each would be a god node carrying no project information —
            # the same reason Java's java.lang and Python's typing names are
            # suppressed. In-corpus `%` classes still resolve above; `%` ROUTINES
            # (%ERPUTIL, %ERPUI) are NOT suppressed, because those are real
            # application code that merely lives in another repository.
            continue

        if kind == "include":
            include = str(rc.get("include") or "")
            if not include:
                continue
            candidates = index.include_files.get(_canonical_name(include), [])
            if candidates:
                file_nid = _pick_nearest(candidates, caller_file)
                if file_nid is not None:
                    emit(rc, file_nid)
            else:
                emit(rc, external_nid(include))
