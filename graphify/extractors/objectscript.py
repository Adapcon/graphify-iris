"""InterSystems IRIS ObjectScript extractor (`.mac` routines, `.cls` classes, `.inc` includes).

There is no ObjectScript tree-sitter grammar on PyPI, so this is a line-oriented
regex extractor, the same shape as `extractors/apex.py`.

What ONE file can decide by itself, and what it cannot:

* `$$Label(...)`, `do Label`, `goto Label` and (inside a class) `..Method()` name
  something declared in the file being extracted. Those are resolved here,
  against the routine's own label table / the class's own member table.
* `$$Label^ROUTINE(...)`, `do Label^ROUTINE`, `do ^ROUTINE`,
  `##class(Pkg.Cls).Method()`, `Extends`, `As <type>` and `#include MACROS` name
  a ROUTINE, a CLASS or an INCLUDE FILE — never a path. Which file holds it is a
  corpus-level question (a routine's on-disk location follows no rule the callee
  name encodes), so each of those is emitted as a `raw_calls` record and bound to
  a real node — or to one shared `ref` external node per name — by
  `graphify.objectscript_resolution`, which runs from the language-resolver
  registry after every file has been extracted.

Two IRIS-specific naming facts the extractors and the resolver both depend on:

* Percent names (`%ERPUTIL`, `%ERPMACROS`) are exported to disk with the `%`
  replaced by `_` (`_ERPUTIL.mac`), so routine/include lookups must compare
  `_canonical_name()` forms, not raw names.
* A class is addressed by its declared dotted name (`Erp.Order`), not by its
  file name, so class lookups go through the class node's label.
* That name is written unqualified whenever the target is in reach: `##class(Item)`
  inside `Erp.Order` means `Erp.Item`, and `Import Erp.Util` puts another package in
  reach the same way. Only the referencing file knows its own package and imports,
  so every class `raw_call` carries that search path (`_class_search_path`) for the
  resolver to expand.

`.cls` is shared with Salesforce Apex and `.inc` with Pascal; `extract._get_extractor`
sniffs content (see `_is_objectscript_class` / `_is_objectscript_include`) before
routing either suffix here.
"""
from __future__ import annotations

import re
from pathlib import Path

from graphify.extractors.base import _file_stem, _make_id

# Stamped on every raw_call so the resolver can claim only its own records —
# `.cls`/`.inc` are shared suffixes, so raw_calls from several languages coexist.
LANG = "objectscript"

_SNIFF_BYTES = 256 * 1024

# ── name shapes ──────────────────────────────────────────────────────────────
# Routine / macro / member name: letters and digits, optional leading `%` for
# system names. `_QNAME` additionally allows dots (class names, `^A.B` globals).
# Labels get their own, wider shape: they may START with a digit — screen-driven
# ERP routines conventionally name their sections `0000`, `1100ON`, `2999`.
_NAME = r"%?[A-Za-z][A-Za-z0-9]*"
_QNAME = r"%?[A-Za-z][A-Za-z0-9.]*"
_LABEL = r"[%A-Za-z0-9][A-Za-z0-9]*"

# Commands whose argument is a routine/label reference rather than an expression.
# Needed because `do ^ROU` (routine) and `set x=^GLO` (global) differ only by the
# command in front of the caret. Postconditions (`do:$$$ISERR(sc) ^ROU`) attach
# to the command with a colon, hence the optional `:<no-space-run>`.
_CALL_CMD = r"(?:do|d|goto|g|job|j)(?::\S+?)?"

_ROUTINE_HEADER_RE = re.compile(rf"^ROUTINE\s+(?P<name>{_NAME})", re.IGNORECASE)
# A label owns column 0; the routine body is indented. The name must be followed
# by `(` (formal parameters), whitespace/tab (the conventional `Label\t;` form)
# or end of line.
_LABEL_DEF_RE = re.compile(rf"^(?P<name>{_LABEL})(?=\(|\s|$)")

# `$$Label^ROUTINE(...)` (extrinsic function) and `Label^ROUTINE` (the argument
# of do/goto/job). One pattern covers both: the `$$` is optional. The lookbehind
# keeps the label from starting mid-identifier and rejects `$$$Macro^` (a macro,
# not a call); `_` is deliberately NOT excluded — it is ObjectScript's
# concatenation operator, so `"x"_$$Fn^ROU()` is a real call site.
_QUALIFIED_CALL_RE = re.compile(
    rf"(?<![A-Za-z0-9.%^$])(?P<extrinsic>\$\$)?(?P<label>{_LABEL})\^(?P<routine>{_QNAME})"
)
# `do ^ROUTINE` / `goto ^ROU` — whole-routine call, no entry label.
_UNQUALIFIED_CALL_RE = re.compile(rf"\b{_CALL_CMD}\s+\^(?P<routine>{_QNAME})", re.IGNORECASE)
# `$$Label(...)` with no `^`: a label in THIS routine. `(?!\$)` after `$$` keeps
# `$$$Macro(...)` out, and the trailing `(?![A-Za-z0-9^])` forces the maximal
# name so `$$Foo^ROU` can never match as local `$$Fo`.
_LOCAL_EXTRINSIC_RE = re.compile(rf"(?<!\$)\$\$(?!\$)(?P<label>{_LABEL})(?![A-Za-z0-9^])")
# `do Label`, `goto 1100`, `job Label(args)` — again a label in THIS routine.
_LOCAL_CALL_RE = re.compile(rf"\b{_CALL_CMD}\s+(?P<label>{_LABEL})(?![A-Za-z0-9^])", re.IGNORECASE)
# `##class(Pkg.Cls).Method(...)`, `##class(Pkg.Cls).%New()`, or a bare
# `##class(Pkg.Cls)` used as a class reference.
_CLASS_CALL_RE = re.compile(
    rf"##class\s*\(\s*(?P<cls>{_QNAME})\s*\)(?:\s*\.\s*(?P<member>{_NAME}))?",
    re.IGNORECASE,
)
# `..Method()` / `..property` — a member of the class being extracted.
_DOT_MEMBER_RE = re.compile(rf"\.\.(?P<member>{_NAME})")
# `^GLOBAL`, `^%ERPRATE(1,2)`, `^|"SAMPLES"|GLO`. The lookbehind excludes
# `Label^ROU` (a routine reference, where the caret follows a name) and `^$JOB`
# style structured references.
_GLOBAL_RE = re.compile(rf"(?<![A-Za-z0-9.%^$])\^(?:\|[^|]*\|\s*)?(?P<name>{_QNAME})")
# A string that is nothing but `Label^ROUTINE` is a framework callback target
# (`do Confirm^%ERPUI(,,,"Are you sure?",,"9100SN^ORDER680")`), i.e. a real call
# edge that only shows up as data. Emitted as INFERRED, unlike the syntactic
# call forms. The label is REQUIRED: a callback names an entry point, whereas a
# bare `"^NAME"` string names a GLOBAL passed as data — the `@glb@(sub)`
# indirection idiom, and helpers that take global NAMES as arguments
# (`do CompareGlobals^%ERPTEST("^||APPLOG","^%APPLOG")`). Reading those as calls
# invents an edge to whatever routine happens to share the name, and a log global
# commonly has a same-named routine maintaining it (`^%APPLOG` the global vs
# `_APPLOG.mac` the routine), so they go to _GLOBAL_STRING_RE instead.
_CALLBACK_STRING_RE = re.compile(rf"^(?P<label>{_LABEL})\^(?P<routine>{_QNAME})$")
# `"^%APPLOG"`, `"^||APPLOG"`, `"^|""SAMPLES""|GLO"`: a global named as data.
_GLOBAL_STRING_RE = re.compile(rf"^\^(?:\|[^|]*\|\s*)?(?P<name>{_QNAME})$")

_INCLUDE_RE = re.compile(r"^\s*#include\s+(?P<names>[^;]+)", re.IGNORECASE)
_MACRO_DEF_RE = re.compile(rf"^\s*#(?:define|def1arg)\s+(?P<name>{_NAME})", re.IGNORECASE)

# ── class-definition (.cls) declarations ─────────────────────────────────────
_CLASS_DECL_RE = re.compile(rf"^\s*Class\s+(?P<name>{_QNAME})(?P<rest>.*)$")
_EXTENDS_RE = re.compile(rf"\bExtends\s+(?:\((?P<many>[^)]*)\)|(?P<one>{_QNAME}))", re.IGNORECASE)
# Member kinds that own an ObjectScript body (`{ ... }`) and can therefore be
# the caller of a call site; every other kind is a declaration only.
_BEHAVIOR_KINDS = frozenset({
    "classmethod", "method", "clientmethod", "clientclassmethod", "query", "trigger",
})
_MEMBER_RE = re.compile(
    r"^\s*(?P<kind>ClassMethod|ClientClassMethod|ClientMethod|Method|Query|Trigger"
    r"|Property|Relationship|Parameter|Index|ForeignKey|XData|Storage|Projection)"
    rf"\s+(?P<name>{_NAME})(?P<rest>.*)$"
)
# `As <type>`, including the collection forms `As list Of <type>` and
# `As array Of <type>`. `list`/`array` there are IRIS syntax, not classes, so the
# ELEMENT class is what the declaration actually references — capturing `list`
# instead would both lose that reference and mint one `list` node every
# collection property in the corpus points at.
_AS_TYPE_RE = re.compile(rf"\bAs\s+(?:(?i:list|array)\s+(?i:of)\s+)?(?P<type>{_QNAME})")
# `Import Erp.Util` / `Import (Erp.Util, Erp.Sales)` above the class
# declaration: the packages an unqualified class name inside this file may resolve
# against (see `_class_search_path`).
_IMPORT_RE = re.compile(rf"^\s*Import\s+(?:\((?P<many>[^)]*)\)|(?P<one>{_QNAME}))")
# Property/parameter/return types worth an edge: a `%`-prefixed name is IRIS
# library scaffolding (%String, %Integer, %Status, %Library.Persistent) that
# every class in the corpus mentions, so edges to it are pure god-node noise.
# `Extends` keeps its `%` bases — a base class is structural, not an annotation.


def _is_system_type(name: str) -> bool:
    return name.startswith("%")


def _is_routine_shaped(name: str) -> bool:
    """Whether a name can be an IRIS ROUTINE name rather than an ordinary word.

    `^` is also the conventional `$piece` delimiter in this dialect (the `$$$VAR`
    idiom sets `z="^"`), so a DATA list like `$piece("Nao^Sim",z,i)` or
    `$piece("Percentual^Valor",z,i)` has the exact shape of a `Label^ROUTINE`
    callback string, and reading it as one invents a call to a routine named `Sim`.
    A routine name is upper-case, optionally with digits and a lower-case variant
    suffix (`CCEPI160a`, `CCCGI999global`); a capitalized word is not. Measured over
    68,545 routines in a real ERP workspace: 0.16% carry any lower-case letter, and
    every one of those also carries digits — so requiring "no lower-case, or has a
    digit" keeps every real name and rejects the prose.

    Only the string-literal heuristic needs this. A syntactic `do Label^MyRoutine`
    is a call whatever its spelling.
    """
    core = name.lstrip("%")
    if not core:
        return False
    return any(c.isdigit() for c in core) or core.isupper()


def _canonical_name(name: str) -> str:
    """Fold an IRIS routine/include name to its lookup key.

    A percent routine is exported to disk with `%` rewritten to `_`
    (`%ERPUTIL` -> `_ERPUTIL.mac`), and callers write it either way, so both
    forms — and the file stem — must land on one key. Case is folded too: the
    on-disk name is what the resolver indexes, and exports are not consistent
    about it.
    """
    return name.lstrip("%_").casefold()


def _read_source(path: Path) -> str | None:
    """Read a source file, or None when it cannot be read.

    `errors="replace"` because IRIS studio exports are frequently CP1252: a
    mangled accented character inside a comment or a message string never
    changes what the structural regexes match. `utf-8-sig` strips a BOM when one
    is present (some export tools write one) — left in place it would sit in front
    of a column-0 `ROUTINE`/`Class`/label and hide the first declaration in the
    file, since every one of those is anchored at column 0.
    """
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


def _mask_noncode(line: str, in_block_comment: bool) -> tuple[str, list[str], bool]:
    """Blank out string literals and comments, preserving character offsets.

    Returns `(masked_line, string_literals, still_in_block_comment)`. Every
    removed character becomes a space so a match offset in the masked line still
    points at the same column of the original. ObjectScript comments are `;` to
    end of line, `//` to end of line and `/* ... */` across lines; a quote inside
    a string is escaped by doubling it (two quote characters in a row).

    String literals are returned rather than discarded because the framework
    passes callbacks as `"Label^ROUTINE"` strings, which are call sites.
    """
    out: list[str] = []
    strings: list[str] = []
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if in_block_comment:
            if line.startswith("*/", i):
                in_block_comment = False
                out.append("  ")
                i += 2
                continue
            out.append(" ")
            i += 1
            continue
        if ch == '"':
            j = i + 1
            buf: list[str] = []
            while j < n:
                if line[j] == '"':
                    if j + 1 < n and line[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    j += 1
                    break
                buf.append(line[j])
                j += 1
            strings.append("".join(buf))
            out.append(" " * (j - i))
            i = j
            continue
        if line.startswith("/*", i):
            in_block_comment = True
            out.append("  ")
            i += 2
            continue
        if line.startswith("//", i) or ch == ";":
            out.append(" " * (n - i))
            break
        out.append(ch)
        i += 1
    return "".join(out), strings, in_block_comment


class _Sink:
    """Node/edge/raw_call accumulator for one file.

    A small class rather than the usual closures because the three extractors
    (`.mac`, `.cls`, `.inc`) share one call scanner: the scanner needs the
    emitters, and passing four closures around is worse than passing this.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.str_path = str(path)
        self.stem = _file_stem(path)
        # Packages an unqualified class name in this file resolves against, in
        # order, comma-joined. Set by `extract_objectscript_class`; empty for a
        # routine or an include, which have no package context.
        self.pkgs = ""
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self.raw_calls: list[dict] = []
        self.seen_ids: set[str] = set()
        self._seen_edges: set[tuple[str, str, str, str]] = set()
        self.file_nid = _make_id(self.str_path)
        self.add_node(self.file_nid, path.name, 1)

    def add_node(self, nid: str, label: str, line: int) -> None:
        if not nid or nid in self.seen_ids:
            return
        self.seen_ids.add(nid)
        self.nodes.append({
            "id": nid,
            "label": label,
            "file_type": "code",
            "source_file": self.str_path,
            "source_location": f"L{line}",
        })

    def add_shared_node(self, nid: str, label: str) -> None:
        """A node that is not OWNED by this file: an IRIS global.

        Sourceless on purpose — `^%ERPRATE` is one entity referenced from every
        routine that touches it, and a source_file would make the colliding-id
        pass split it into one copy per referencing file.
        """
        if not nid or nid in self.seen_ids:
            return
        self.seen_ids.add(nid)
        self.nodes.append({
            "id": nid,
            "label": label,
            "file_type": "code",
            "source_file": "",
            "source_location": "",
        })

    def add_edge(self, src: str, tgt: str, relation: str, line: int,
                 *, confidence: str = "EXTRACTED", context: str | None = None) -> None:
        if not src or not tgt or src == tgt:
            return
        key = (src, tgt, relation, context or "")
        if key in self._seen_edges:
            return
        self._seen_edges.add(key)
        edge = {
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": confidence,
            "source_file": self.str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        }
        if context:
            edge["context"] = context
        self.edges.append(edge)

    def add_raw_call(self, caller_nid: str, line: int, **fields: str) -> None:
        """Record a cross-file reference for `graphify.objectscript_resolution`.

        Shape (all string-valued): `lang`/`kind`/`relation`/`context`, plus
        `routine`+`label` for kind `routine`, `cls`+`member` for kind `class`,
        `include` for kind `include`, and `confidence`. A kind `class` record
        additionally carries `pkgs` (this file's package search path) whenever the
        file has one, since `##class(Item)` inside `Erp.Order` means
        `Erp.Item` and only the referencing file knows that.
        """
        if fields.get("kind") == "class" and self.pkgs:
            fields.setdefault("pkgs", self.pkgs)
        self.raw_calls.append({
            "lang": LANG,
            "caller_nid": caller_nid,
            "source_file": self.str_path,
            "source_location": f"L{line}",
            **fields,
        })

    def result(self) -> dict:
        return {"nodes": self.nodes, "edges": self.edges, "raw_calls": self.raw_calls}


def _global_nid(name: str) -> str:
    # Own namespace: a global is not a file symbol, and `^CSAL` must not be able
    # to collide with a routine or class node id.
    return _make_id("iris", "global", name)


_WRITE_PREFIX_RE = re.compile(
    r"(?:\bkill|\bk|\bzkill|\bzk|\bmerge|\bm|\$\$\$(?:Set|Kill|Merge)[A-Za-z0-9]*)\s*\(?\s*\.?$",
    re.IGNORECASE,
)


def _skip_subscript(code: str, pos: int) -> int:
    """Index just past a global's `( ... )` subscript list, if it has one."""
    if pos >= len(code) or code[pos] != "(":
        return pos
    depth = 0
    for i in range(pos, len(code)):
        if code[i] == "(":
            depth += 1
        elif code[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(code)


def _is_global_write(code: str, start: int, end: int) -> bool:
    """Whether a global reference at `code[start:end]` is being written.

    Heuristic, and deliberately biased toward "read": an assignment is
    recognized by an `=` right after the (optionally subscripted) reference
    (`set ^%ERPRATE(1)=d`), and a destructive/indirect write by the command in
    front of it (`kill ^%ERPRATE`, `merge ^A=^B`, and any `$$$Set…`/`$$$Kill…`/
    `$$$Merge…` wrapper macro that takes the global as its first argument, where
    the assignment happens inside the macro so no `=` is visible at the call site).
    """
    after = _skip_subscript(code, end)
    if code[after:after + 1] == "=" and code[after:after + 2] != "==":
        return True
    return bool(_WRITE_PREFIX_RE.search(code[:start]))


def _scan_line(
    sink: _Sink,
    code: str,
    strings: list[str],
    lineno: int,
    caller_nid: str,
    *,
    labels: dict[str, str] | None = None,
    members: dict[str, tuple[str, str]] | None = None,
) -> None:
    """Emit every call / global reference on one already-masked line.

    `labels` maps a lowercased label name in the CURRENT routine to its node id
    (`.mac`); `members` maps a lowercased member name of the current class to
    `(node id, relation)` — `calls` for a method, `references` for a property
    (`.cls`). Both are what makes a same-file reference resolvable here instead
    of being deferred to the resolver.
    """
    consumed: list[tuple[int, int]] = []

    for m in _CLASS_CALL_RE.finditer(code):
        consumed.append(m.span())
        cls_name = m.group("cls")
        member = m.group("member") or ""
        sink.add_raw_call(
            caller_nid, lineno,
            kind="class", relation="calls" if member else "references",
            context="class_method" if member else "class_ref",
            cls=cls_name, member=member, confidence="EXTRACTED",
        )

    for m in _QUALIFIED_CALL_RE.finditer(code):
        consumed.append(m.span())
        sink.add_raw_call(
            caller_nid, lineno,
            kind="routine", relation="calls",
            context="extrinsic_function" if m.group("extrinsic") else "routine_call",
            routine=m.group("routine"), label=m.group("label"), confidence="EXTRACTED",
        )

    for m in _UNQUALIFIED_CALL_RE.finditer(code):
        consumed.append(m.span())
        sink.add_raw_call(
            caller_nid, lineno,
            kind="routine", relation="calls", context="routine_call",
            routine=m.group("routine"), label="", confidence="EXTRACTED",
        )

    if labels:
        for m in _LOCAL_EXTRINSIC_RE.finditer(code):
            target = labels.get(m.group("label").casefold())
            if target:
                sink.add_edge(caller_nid, target, "calls", lineno, context="extrinsic_function")
        for m in _LOCAL_CALL_RE.finditer(code):
            target = labels.get(m.group("label").casefold())
            if target:
                sink.add_edge(caller_nid, target, "calls", lineno, context="call")

    if members:
        for m in _DOT_MEMBER_RE.finditer(code):
            member = members.get(m.group("member").casefold())
            if member:
                target, relation = member
                sink.add_edge(caller_nid, target, relation, lineno, context="self_member")

    for m in _GLOBAL_RE.finditer(code):
        start, end = m.span()
        # `do ^ROU` / `Label^ROU` already claimed this caret as a routine
        # reference; the same text must not also count as a global.
        if any(cs <= start < ce for cs, ce in consumed):
            continue
        name = m.group("name")
        nid = _global_nid(name)
        sink.add_shared_node(nid, f"^{name}")
        context = "global_write" if _is_global_write(code, start, end) else "global_read"
        sink.add_edge(caller_nid, nid, "references", lineno, context=context)

    for literal in strings:
        text = literal.strip()
        cb = _CALLBACK_STRING_RE.match(text)
        if cb and _is_routine_shaped(cb.group("routine")):
            # Always deferred, even when the label exists in THIS file: the string
            # names its routine explicitly, and a same-named label here would
            # otherwise hijack a callback aimed at another routine. The resolver
            # binds it back to this file when the routine name is this file's own.
            sink.add_raw_call(
                caller_nid, lineno,
                kind="routine", relation="calls", context="callback_string",
                routine=cb.group("routine"), label=cb.group("label"),
                confidence="INFERRED",
            )
            continue
        gs = _GLOBAL_STRING_RE.match(text)
        if gs:
            # INFERRED, not EXTRACTED: the string is the name of a global the code
            # goes on to use indirectly, so the dependency is real but this line
            # is not itself the read or the write — hence its own context rather
            # than global_read / global_write.
            name = gs.group("name")
            nid = _global_nid(name)
            sink.add_shared_node(nid, f"^{name}")
            sink.add_edge(
                caller_nid, nid, "references", lineno,
                confidence="INFERRED", context="global_name_string",
            )


def _emit_includes(sink: _Sink, code: str, lineno: int, caller_nid: str) -> bool:
    """Emit `#include %MACROS` / `#include (%A,%B)` refs. True when the line was one."""
    m = _INCLUDE_RE.match(code)
    if not m:
        return False
    names = m.group("names").strip().strip("()")
    for raw in names.split(","):
        name = raw.strip()
        if name:
            sink.add_raw_call(
                caller_nid, lineno,
                kind="include", relation="imports", context="include",
                include=name, confidence="EXTRACTED",
            )
    return True


def extract_objectscript(path: Path) -> dict:
    """Extract labels, calls, global references and `#include`s from an IRIS `.mac` routine.

    Nodes: the routine file, plus one node per label (`Label()`), linked
    `defines`. Edges: `calls` between labels of this routine, `references` to
    `^GLOBAL` nodes (context `global_read` / `global_write`). Everything that
    names another routine, a class or an include file leaves as a `raw_calls`
    record for `graphify.objectscript_resolution`.

    Example: `extract_objectscript(Path("src/routines/ORDER680RG.mac"))`.
    """
    text = _read_source(path)
    if text is None:
        return {"nodes": [], "edges": [], "raw_calls": []}

    sink = _Sink(path)
    lines = text.splitlines()

    # Pass 1: the label table. A label can be called from a line above its own
    # declaration, so it must be complete before any call is resolved. Matched
    # against the MASKED line so text inside a `/* ... */` block cannot be read
    # as a label declaration.
    labels: dict[str, str] = {}
    label_nid_by_line: dict[int, str] = {}
    in_block_comment = False
    for lineno, line in enumerate(lines, start=1):
        raw, _strings, in_block_comment = _mask_noncode(line, in_block_comment)
        if _ROUTINE_HEADER_RE.match(raw):
            continue
        m = _LABEL_DEF_RE.match(raw)
        if not m:
            continue
        name = m.group("name")
        nid = _make_id(sink.stem, name)
        sink.add_node(nid, f"{name}()", lineno)
        sink.add_edge(sink.file_nid, nid, "defines", lineno, context="label")
        labels.setdefault(name.casefold(), nid)
        label_nid_by_line[lineno] = nid

    # Pass 2: calls, globals and includes, attributed to the label they sit under
    # (the file node for anything above the first label).
    scope = sink.file_nid
    in_block_comment = False
    for lineno, raw in enumerate(lines, start=1):
        scope = label_nid_by_line.get(lineno, scope)
        code, strings, in_block_comment = _mask_noncode(raw, in_block_comment)
        if not code.strip():
            continue
        if _emit_includes(sink, code, lineno, sink.file_nid):
            continue
        _scan_line(sink, code, strings, lineno, scope, labels=labels)

    return sink.result()


def _parse_class_members(lines: list[str]) -> tuple[dict | None, list[dict]]:
    """Collect the `Class` declaration and its member declarations.

    Returns `(class_info, members)`, where `class_info` is
    `{"name", "line", "bases", "imports"}` (None when the file has no class
    declaration) and each member is `{"kind", "name", "line", "type"}`.
    """
    class_info: dict | None = None
    members: list[dict] = []
    imports: list[str] = []
    in_block_comment = False
    depth = 0
    for lineno, raw in enumerate(lines, start=1):
        code, _strings, in_block_comment = _mask_noncode(raw, in_block_comment)
        opens, closes = code.count("{"), code.count("}")
        # `Import` precedes the class declaration, so it is collected on the way
        # to it rather than at declaration depth.
        if class_info is None:
            im = _IMPORT_RE.match(code)
            if im:
                raw_pkgs = im.group("many") or im.group("one") or ""
                imports.extend(p.strip() for p in raw_pkgs.split(",") if p.strip())
                continue
        # `Class X ...` sits at depth 0; its members sit at depth 1 (inside the
        # class body's braces); a method body pushes depth to 2 and holds only
        # code. So declarations are read at exactly one depth, and everything
        # deeper is skipped.
        declaration_depth = 1 if class_info is not None else 0
        if depth == declaration_depth:
            if class_info is None:
                cm = _CLASS_DECL_RE.match(code)
                if cm:
                    bases: list[str] = []
                    em = _EXTENDS_RE.search(cm.group("rest"))
                    if em:
                        raw_bases = em.group("many") or em.group("one") or ""
                        bases = [b.strip() for b in raw_bases.split(",") if b.strip()]
                    class_info = {"name": cm.group("name"), "line": lineno, "bases": bases}
                    depth = max(depth + opens - closes, 0)
                    continue
            mm = _MEMBER_RE.match(code)
            if mm:
                type_match = _AS_TYPE_RE.search(mm.group("rest"))
                members.append({
                    "kind": mm.group("kind").casefold(),
                    "name": mm.group("name"),
                    "line": lineno,
                    "type": type_match.group("type") if type_match else "",
                })
        depth = max(depth + opens - closes, 0)
    if class_info is not None:
        class_info["imports"] = imports
    return class_info, members


def _class_search_path(class_name: str, imports: list[str]) -> str:
    """Packages an unqualified class name in this file resolves against, in order.

    IRIS resolves a class name with no package against the referencing class's OWN
    package first, then the packages named in its `Import` directives — so
    `##class(Item)` inside `Class Erp.Order` is `Erp.Item`. Returned
    comma-joined for the `raw_calls` record, whose fields are all strings.
    """
    own = class_name.rpartition(".")[0]
    path = [own] if own else []
    path += [pkg for pkg in imports if pkg and pkg != own]
    return ",".join(path)


def extract_objectscript_class(path: Path) -> dict:
    """Extract a class, its members and its calls from an IRIS `.cls` file.

    Nodes: the file, the class (labelled with its declared dotted name,
    `Sales.Order`), one `.Method()` node per method/query/trigger and one
    `.property` node per property/relationship. Edges: `contains` (file→class),
    `method`, `defines` (context `property`), `calls` for `..Member()`
    self-calls, and `references` to `^GLOBAL` nodes — including the globals named
    in the `Storage` block, which is how a persistent class declares the global
    it maps onto. `Extends` bases, `As <type>` references and `##class(...)`
    calls leave as `raw_calls` for `graphify.objectscript_resolution`.

    Example: `extract_objectscript_class(Path("src/classes/Sales/Order.cls"))`.
    """
    text = _read_source(path)
    if text is None:
        return {"nodes": [], "edges": [], "raw_calls": []}

    sink = _Sink(path)
    lines = text.splitlines()
    class_info, member_decls = _parse_class_members(lines)

    if class_info is None:
        # No class declaration (an empty or truncated export): the file node
        # alone, so the file is still represented in the graph.
        return sink.result()

    class_name = str(class_info["name"])
    sink.pkgs = _class_search_path(class_name, list(class_info["imports"]))
    class_nid = _make_id(sink.stem, class_name)
    sink.add_node(class_nid, class_name, int(class_info["line"]))
    sink.add_edge(sink.file_nid, class_nid, "contains", int(class_info["line"]))

    for base in class_info["bases"]:
        sink.add_raw_call(
            class_nid, int(class_info["line"]),
            kind="class", relation="inherits", context="extends",
            cls=base, member="", confidence="EXTRACTED",
        )

    # Member nodes, plus the `..Member` lookup table used while scanning bodies.
    members: dict[str, tuple[str, str]] = {}
    body_scope_by_line: dict[int, str] = {}
    for decl in member_decls:
        kind, name, line = str(decl["kind"]), str(decl["name"]), int(decl["line"])
        if kind in _BEHAVIOR_KINDS:
            nid = _make_id(class_nid, name)
            sink.add_node(nid, f".{name}()", line)
            sink.add_edge(class_nid, nid, "method", line, context=kind)
            members.setdefault(name.casefold(), (nid, "calls"))
            body_scope_by_line[line] = nid
        elif kind in ("property", "relationship"):
            nid = _make_id(class_nid, name)
            sink.add_node(nid, f".{name}", line)
            sink.add_edge(class_nid, nid, "defines", line, context="property")
            # `..codigo` reads or writes a property; it is not a call.
            members.setdefault(name.casefold(), (nid, "references"))
        # Parameter / Index / ForeignKey / XData / Storage / Projection are
        # declaration metadata, not behavior or data model: no node of their own.
        type_name = str(decl["type"])
        # A `%`-prefixed member type is an IRIS datatype annotation (%String,
        # %Integer, %Library.Status). Every class in a corpus names them, so
        # edges to them are pure god-node noise — the same call made for Java's
        # java.lang / Python's typing noise lists. `Extends` and `##class()` are
        # NOT filtered here: those are structural, so they are deferred to the
        # resolver, which keeps them when the class is really in the corpus.
        if type_name and not _is_system_type(type_name):
            sink.add_raw_call(
                class_nid, line,
                kind="class", relation="references",
                context=f"{kind}_type" if kind in ("property", "relationship") else "member_type",
                cls=type_name, member="", confidence="EXTRACTED",
            )

    # Scan every line for call sites, attributing each to the member whose body
    # it sits in — the class itself for declaration-level expressions and for the
    # `Storage` block, whose `<DataLocation>^GLOBAL</DataLocation>` is how a
    # persistent class names the global it maps onto.
    scope = class_nid
    pending_scope: str | None = None
    member_body_depth: int | None = None
    depth = 0
    in_block_comment = False
    for lineno, raw in enumerate(lines, start=1):
        code, strings, in_block_comment = _mask_noncode(raw, in_block_comment)
        if lineno in body_scope_by_line:
            pending_scope = body_scope_by_line[lineno]
        opens, closes = code.count("{"), code.count("}")
        if opens and pending_scope is not None:
            # The `{` that opens the body of the member declared just above (same
            # line for a one-line body).
            scope = pending_scope
            member_body_depth = depth + 1
            pending_scope = None
        if code.strip() and not _emit_includes(sink, code, lineno, sink.file_nid):
            _scan_line(sink, code, strings, lineno, scope, members=members)
        depth = max(depth + opens - closes, 0)
        if member_body_depth is not None and depth < member_body_depth:
            scope = class_nid
            member_body_depth = None

    return sink.result()


def extract_objectscript_include(path: Path) -> dict:
    """Extract macro definitions and nested `#include`s from an IRIS `.inc` file.

    Nodes: the file, plus one `$$$MACRO` node per `#define` / `#def1arg`, linked
    `defines`. Macro BODIES are templates expanded at compile time in the caller's
    context, so they are not scanned for calls or globals — the expansion belongs
    to the including routine, not to the include file.

    Example: `extract_objectscript_include(Path("src/include/_ERPMACROS.inc"))`.
    """
    text = _read_source(path)
    if text is None:
        return {"nodes": [], "edges": [], "raw_calls": []}

    sink = _Sink(path)
    in_block_comment = False
    for lineno, raw in enumerate(text.splitlines(), start=1):
        code, _strings, in_block_comment = _mask_noncode(raw, in_block_comment)
        if not code.strip():
            continue
        if _emit_includes(sink, code, lineno, sink.file_nid):
            continue
        m = _MACRO_DEF_RE.match(code)
        if not m:
            continue
        name = m.group("name")
        nid = _make_id(sink.stem, name)
        sink.add_node(nid, f"$$${name}", lineno)
        sink.add_edge(sink.file_nid, nid, "defines", lineno, context="macro")

    return sink.result()


# ── content sniffing for the suffixes shared with other languages ────────────
# `.cls` is Salesforce Apex in the suffix map and `.inc` is Pascal; both keep
# that routing unless the file carries a marker that is ILLEGAL in the other
# language, so a corpus with no IRIS in it is unaffected.

# `Class Pkg.Name [Extends ...]` at column 0 with a capital C. Apex spells its
# declaration `public class Foo` / `global class Foo` — lower-case keyword, and
# always preceded by an access modifier — so this cannot match Apex. `ClassMethod`
# and `##class(` are ObjectScript-only and cover exports whose class line is
# unusual (e.g. wrapped attributes).
_OS_CLASS_MARKERS = (
    re.compile(rf"^Class\s+{_QNAME}", re.MULTILINE),
    re.compile(r"^\s*ClassMethod\s+", re.MULTILINE),
    re.compile(r"##class\s*\(", re.IGNORECASE),
)
# `#define`/`#include`/`#if` at line start: C-style preprocessor directives that
# Pascal (which spells them `{$DEFINE}` / `{$I file}`) cannot contain.
_OS_INCLUDE_MARKER = re.compile(
    r"^\s*#(?:define|def1arg|include|import|if|ifdef|ifndef|else|endif)\b",
    re.IGNORECASE | re.MULTILINE,
)
# ...but a C/C++ `.inc` carries the same directives, so the marker alone is not
# enough. C names its include target as a PATH (`#include <stdio.h>`,
# `#include "vec.h"`) where IRIS names a routine (`#include %ERPMACROS`), and
# `#pragma` has no ObjectScript counterpart. Either one means the file is not an
# IRIS include and keeps its existing (Pascal) routing.
_C_INCLUDE_MARKER = re.compile(r"^\s*#\s*(?:include\s*[<\"]|pragma\b)", re.MULTILINE)


def _sniff_text(path: Path) -> str:
    # `utf-8-sig` for the same reason as `_read_source`: a BOM in front of a
    # column-0 `Class` line would make an IRIS class read as Apex.
    try:
        return path.read_bytes()[:_SNIFF_BYTES].decode("utf-8-sig", errors="replace")
    except OSError:
        return ""


def _is_objectscript_class(path: Path) -> bool:
    """Whether a `.cls` file is IRIS ObjectScript rather than Salesforce Apex."""
    head = _sniff_text(path)
    return any(marker.search(head) for marker in _OS_CLASS_MARKERS)


def _is_objectscript_include(path: Path) -> bool:
    """Whether a `.inc` file is an IRIS macro include rather than a Pascal or C one."""
    head = _sniff_text(path)
    return bool(_OS_INCLUDE_MARKER.search(head)) and not _C_INCLUDE_MARKER.search(head)
