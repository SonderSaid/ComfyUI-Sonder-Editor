"""Registration tripwires for the scene-mutation plug-in surface.

`plans/mutation-registration-tripwires.md`. Three things live here: the scanning
machinery (extractors, the client-payload predicate, and the self-checks that
prove the scan is really reading the tree), and two tripwires built on it.

**Rebase policy.** Every scene operation must declare one, because
`_rebaseSceneMutationIntentForHistory` ends in `default: break` and an
unregistered operation is retargeted by nothing with no one told.

**Guard coverage.** Every emission carrying a payload the client computed must
carry an `expected*` identity guard or be catalogued with a reason, because
without one the server applies a stale value against whatever document it loads
rather than refusing — the shape of the Critical split defect.

Why the machinery needs its own self-checks. A tripwire built on a scanner that
silently matches nothing is worse than no tripwire, and the shapes that defeat a
naive scan are not hypothetical: an operation literal can pass geometry as ES6
shorthand (`{ …, frame }`), carry its guard as shorthand (`{ …, expected }`),
hide its payload behind an identifier (`fields: body`), or sit inside a comment.
Every one of those appears in the real tree or is scheduled to. So the self-checks
below are the deliverable, not scaffolding around it.

What this provably cannot do. It reads source text, so it establishes that a key
is written, never that its value is populated at runtime — several guards are
conditional spreads. It classifies a client-computed payload by key name, so a
payload under a name not listed here is invisible until someone adds it. Regex
literals are not masked, so a regex containing an unbalanced brace inside an
operation literal would still confuse the extent matcher; nothing like that
exists today and `test_operation_literal_extents_are_bounded` is what would
notice. Behavioural coverage lives in `test_project_mutation_queue.py` and
`test_history_optimistic.py`.
"""
import ast
import functools
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

ROUTES = ROOT / "server/routes.py"
WIDGET = ROOT / "web/js/editor_widget.js"
JS_DIR = ROOT / "web/js"

# Modules that emit a scene-mutation operation literal. This is an expectation,
# not an input filter: `test_the_emitting_module_set_is_closed` globs every file
# under web/js and fails when the real set differs, so a new emitter cannot
# escape the scan by not being listed here.
EMITTING_MODULES = (
    "editor_widget.js",
    "editor_prompt_panel.js",
    "editor_reference_panel.js",
    "prompt_context_chips.js",
    "prompt_identity_transactions.js",
)

# Per-module operation-literal counts, and the total length of every extent.
# The count alone cannot see a brace matcher that swallows a neighbour without
# running to EOF — the count is one per anchor and does not move. The extent
# total does move, which is why both are pinned. Update deliberately when adding
# or removing an emission; the failure message names the literals that changed.
EXPECTED_LITERAL_COUNTS = {
    # 82 -> 80 on 2026-09-17: `_updateSceneGlobalChannelsWithinGesture` and
    # `_updateScenePromptWithinGesture` were deleted as unreachable code (umbrella
    # Phase B / L1). They were the only patch-shaped `coalesce: true` emissions.
    "editor_widget.js": 80,
    "editor_prompt_panel.js": 4,
    "editor_reference_panel.js": 3,
    "prompt_context_chips.js": 1,
    "prompt_identity_transactions.js": 1,
}

# Scene-mutation enqueue call sites, pinned for the same reason as the
# literal counts above: a scan that quietly stops matching reports a clean
# surface forever. Update deliberately when adding or removing an enqueue.
EXPECTED_ENQUEUE_SITES = 56

# Geometry the client computed from what it could see. Matched with a trailing
# `[:,}]` so ES6 shorthand counts — `split_clip` passes its frame that way, and a
# colon-only pattern reports the Critical defect's own operation as carrying no
# geometry at all.
GEOMETRY_KEYS = (
    "frame", "start_frame", "end_frame", "timeline_start_frame",
    "timeline_end_frame", "track_index", "lane_index", "target_lane",
    "index", "index_a", "index_b", "from_index", "to_index",
)

# Payloads the client assembled. Keying only on geometry under `fields` would
# miss `replace_prompt_sections { sections }`, which replaces every prompt
# section in the scene wholesale.
PAYLOAD_KEYS = ("fields", "sections", "items", "unit", "profiles", "semantic_units")

_GEOMETRY_RE = re.compile(r"\b(?:%s)\s*[:,}]" % "|".join(GEOMETRY_KEYS))

# Row-identity guards only: `expected`, `expected_a`, `expected_b`, including
# shorthand. Deliberately NOT `expected\w*` — `expected_prompt_template` is a
# project-template content-hash precondition checked before the dispatch
# (routes.py, `_apply_scene_mutation_operation`), not a claim about the row this
# operation names. Treating it as a guard silently certifies
# `_updateSceneGlobalContextWithinGesture`'s `update_scene_fields`, an opaque
# whole-global-prompt write that carries no row identity of its own. (A line
# number was cited here and was already wrong when it was written; scope names
# are what survive an edit above them.)
_GUARD_RE = re.compile(r"\bexpected(?:_[ab])?\s*[:,}]")

_OP_ANCHOR_RE = re.compile(r'\{\s*\n?\s*type:\s*"([a-z_]+)"')

# Words that reach the bare `name(` scope form but never own a block.
_NOT_A_SCOPE = frozenset({
    "if", "for", "while", "switch", "catch", "return", "else", "do", "try",
    "with", "function", "new", "await", "typeof", "delete", "void", "yield",
    "throw", "case", "default", "in", "of", "instanceof",
})


# ---------------------------------------------------------------------------
# Source scanning
# ---------------------------------------------------------------------------
def _code_mask(source: str) -> bytearray:
    """1 where a character is code, 0 inside a string, template or comment.

    Every scan below consults this instead of trusting raw offsets. Without it a
    `}` inside a legal string — a scene name is user-authored text — closes an
    extent early and silently drops keys, and a `{ type: "update_clip" … }`
    example inside a doc comment is adopted as a real emission. Both are live
    risks: the second is scheduled, because the umbrella's Phase D adds a
    contract header to `editor_widget.js`.

    Regex literals are deliberately not tracked; distinguishing `/` division
    from a regex needs token context, and no operation literal contains one.
    """
    mask = bytearray(b"\x01") * len(source)
    index, length = 0, len(source)
    while index < length:
        char = source[index]
        nxt = source[index + 1] if index + 1 < length else ""
        if char == "/" and nxt == "/":
            stop = source.find("\n", index)
            stop = length if stop < 0 else stop
        elif char == "/" and nxt == "*":
            stop = source.find("*/", index + 2)
            stop = length if stop < 0 else stop + 2
        elif char in "'\"`":
            stop = index + 1
            while stop < length:
                if source[stop] == "\\":
                    stop += 2
                    continue
                if source[stop] == char:
                    stop += 1
                    break
                if source[stop] == "\n" and char != "`":
                    break
                stop += 1
        else:
            index += 1
            continue
        for position in range(index, min(stop, length)):
            if source[position] != "\n":
                mask[position] = 0
        index = max(stop, index + 1)
    return mask


def _match_delimiter(source: str, start: int, opener: str, closer: str,
                     mask: bytearray | None = None) -> int:
    """Index of the delimiter closing the one at `start`, or -1."""
    if mask is None:
        mask = _code_mask(source)
    depth = 0
    for index in range(start, len(source)):
        if not mask[index]:
            continue
        if source[index] == opener:
            depth += 1
        elif source[index] == closer:
            depth -= 1
            if depth == 0:
                return index
    return -1


# A named scope: a function declaration, an arrow bound to a binding, or a class
# / object method. Resolution is by containment rather than proximity, so an
# inner helper cannot be reported as the owner of a literal that follows it.
_SCOPE_DECL_RE = re.compile(
    r"(?:^|\n)[ \t]*(?:export\s+)?(?:"
    r"(?:async\s+)?function\s*\*?\s*(?P<fn>\w+)\s*\("
    r"|(?:const|let|var)\s+(?P<const>\w+)\s*=\s*(?:async\s*)?(?:function\s*\*?\s*\w*\s*)?\("
    r"|(?:async\s+)?\*?\s*(?P<method>\w+)\s*\("
    r")")


def _scopes(source: str, mask: bytearray | None = None) -> list[tuple[str, int, int]]:
    """Every named scope as `(name, body_start, body_end)`."""
    if mask is None:
        mask = _code_mask(source)
    found = []
    for match in _SCOPE_DECL_RE.finditer(source):
        name = match.group("fn") or match.group("const") or match.group("method")
        if not name or name in _NOT_A_SCOPE or not mask[match.end() - 1]:
            continue
        paren = source.index("(", match.start(match.lastindex or 0))
        close = _match_delimiter(source, paren, "(", ")", mask)
        if close < 0:
            continue
        body = re.match(r"\s*(?:=>\s*)?\{", source[close + 1:close + 12])
        if not body:
            continue
        brace = close + body.end()
        end = _match_delimiter(source, brace, "{", "}", mask)
        if end < 0:
            continue
        found.append((name, brace, end))
    return found


def _enclosing_scope(scopes: list[tuple[str, int, int]], offset: int) -> str:
    """Innermost named scope containing `offset`, or "" when none does.

    Smallest span is the innermost only while every recorded span is a real
    brace block, which is true as long as `_match_delimiter` is sound; nested
    blocks cannot partially overlap. `test_no_recorded_scope_spans_cross`
    is what holds that up.
    """
    best, best_span = "", None
    for name, start, end in scopes:
        if start <= offset <= end and (best_span is None or end - start < best_span):
            best, best_span = name, end - start
    return best


@functools.lru_cache(maxsize=1)
def _dispatcher_op_types() -> frozenset[str]:
    """Scene op types the backend dispatcher accepts, by AST not by regex."""
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_apply_scene_mutation_operation"):
            found, unconsumed = set(), []
            for compare in ast.walk(node):
                if not (isinstance(compare, ast.Compare)
                        and isinstance(compare.left, ast.Name)
                        and compare.left.id == "op_type"):
                    continue
                for comparator in compare.comparators:
                    if isinstance(comparator, ast.Constant) and isinstance(
                            comparator.value, str):
                        found.add(comparator.value)
                    else:
                        unconsumed.append(ast.unparse(compare))
            assert not unconsumed, (
                "the dispatcher compares op_type in a shape this scan does not "
                "read, so those operations would silently leave every scan "
                f"below: {unconsumed}")
            assert found, "no op types found; the dispatcher walk is blind"
            return frozenset(found)
    raise AssertionError("_apply_scene_mutation_operation not found in routes.py")


def _scan(source: str, module: str) -> list[dict]:
    """Every scene-mutation operation literal in one module's source."""
    op_types = _dispatcher_op_types()
    mask = _code_mask(source)
    scopes = _scopes(source, mask)
    literals = []
    for match in _OP_ANCHOR_RE.finditer(source):
        if match.group(1) not in op_types:
            continue
        brace = source.index("{", match.start())
        if not mask[brace]:
            continue  # a documentation example, not an emission
        end = _match_delimiter(source, brace, "{", "}", mask)
        literals.append({
            "module": module,
            "op_type": match.group(1),
            "offset": brace,
            "line": source.count("\n", 0, brace) + 1,
            "scope": _enclosing_scope(scopes, brace),
            "extent": source[brace:end + 1] if end > 0 else "",
            "mask": mask[brace:end + 1] if end > 0 else bytearray(),
            "terminated": end > 0,
        })
    return literals


@functools.lru_cache(maxsize=1)
def _operation_literals() -> tuple[dict, ...]:
    return tuple(
        literal
        for module in EMITTING_MODULES
        for literal in _scan((JS_DIR / module).read_text(encoding="utf-8"), module))


def _scan_text(source: str, module: str = "fixture.js") -> list[dict]:
    """`_scan` for a synthetic source, so guards-the-guard tests share the path."""
    return _scan(source, module)


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------
def _code_only(extent: str, mask: bytearray | None = None) -> str:
    """`extent` with non-code characters blanked, for pattern matching."""
    if mask is None:
        mask = _code_mask(extent)
    return "".join(char if mask[index] else " " for index, char in enumerate(extent))


def _payload_fingerprint(extent: str, mask: bytearray | None = None) -> tuple[str, ...]:
    """Sorted key paths to depth 2 — the Phase 3 exemption fingerprint.

    Top-level keys alone cannot see the payload. In most exempted sites the
    client-computed geometry lives one level down, inside `fields` / `items` /
    `sections`, so a site could go from `fields: { muted }` to
    `fields: { muted, track_index, timeline_start_frame }` — from a boolean to a
    lane index and a frame — without its fingerprint moving at all.

    A payload key whose value is not an inline object records `<opaque>` instead
    of its children, because there is nothing lexical to record. That is not a
    weakness hidden: the token says so, and it is what makes an opaque site
    distinguishable from a transparent one in the catalogue.
    """
    code = _code_only(extent, mask)
    paths, depth, index = [], 0, 0
    parent, parent_depth = "", -1
    while index < len(code):
        char = code[index]
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
            if depth <= parent_depth:
                parent, parent_depth = "", -1
        elif depth in (1, 2):
            match = re.match(r"([A-Za-z_$][\w$]*)\s*([:,}])", code[index:])
            starts_token = index == 0 or not re.match(r"[\w$.]", code[index - 1])
            if match and starts_token:
                previous = code[:index].rstrip()
                # A ternary's middle operand is also followed by `:`. Without
                # this, `end_frame: ok ? nextStart : -1` reports a phantom key
                # `nextStart`, and the guard tripwire then fires on a scanner
                # artifact rather than on a discarded guard.
                is_key = ((match.group(2) == ":" and not previous.endswith("?"))
                          or (previous and previous[-1] in "{,"))
                if is_key:
                    name = match.group(1)
                    if depth == 1:
                        rest = code[index + match.end():].lstrip()
                        if name in PAYLOAD_KEYS:
                            if match.group(2) != ":" or not rest.startswith("{"):
                                paths.append(f"{name}.<opaque>")
                            elif rest[1:].lstrip().startswith("..."):
                                paths.append(f"{name}.<opaque>")
                            else:
                                parent, parent_depth = name, depth
                        paths.append(name)
                    elif parent:
                        paths.append(f"{parent}.{name}")
                    index += match.end() - 1
                    continue
            # A spread inside a payload object hides whatever it carries. Only a
            # LEADING one was recognised, at the parent level above, so
            # `fields: { muted, ...geometry }` reported `fields.muted` and
            # nothing else -- the readable half certifying the unreadable half,
            # under an exemption whose stated reason is about carrying geometry.
            # No live emission has this shape, so no pinned fingerprint moves.
            elif depth == 2 and parent and code.startswith("...", index):
                paths.append(f"{parent}.<opaque>")
                index += 3
                continue
        index += 1
    return tuple(sorted(set(paths)))


def _top_level_keys(extent: str, mask: bytearray | None = None) -> tuple[str, ...]:
    """Depth-1 key names only. Used for payload-class membership, not pinning."""
    return tuple(sorted({path for path in _payload_fingerprint(extent, mask)
                         if "." not in path}))


def _carries_client_payload(extent: str, mask: bytearray | None = None) -> bool:
    code = _code_only(extent, mask)
    return bool(_GEOMETRY_RE.search(code)) or any(
        key in _top_level_keys(extent, mask) for key in PAYLOAD_KEYS)


def _is_guarded(extent: str, mask: bytearray | None = None) -> bool:
    return bool(_GUARD_RE.search(_code_only(extent, mask)))


@functools.lru_cache(maxsize=1)
def _rebase_switch_block() -> str:
    """The `switch (operation.type) { … }` block of the history rebase method.

    Bounded at the switch rather than at the method, because the method tail
    (`default:`, the loop's closing braces, `return rebased;`) would otherwise be
    handed to the last `case` as its body — and an empty case appended there, the
    one position a developer naturally appends at, would read as doing work.
    """
    source = WIDGET.read_text(encoding="utf-8")
    declarations = re.findall(
        r"^    _rebaseSceneMutationIntentForHistory\(", source, re.M)
    assert len(declarations) == 1, (
        "the rebase method anchor is ambiguous; `source.index` would take the "
        f"first textual hit, which may be a call site: {len(declarations)} found")
    start = source.index("    _rebaseSceneMutationIntentForHistory(")
    brace = source.index("{", start)
    end = _match_delimiter(source, brace, "{", "}")
    assert end > 0, "the rebase method body did not terminate"
    method = source[brace:end + 1]
    switch_at = method.index("switch (operation.type)")
    switch_brace = method.index("{", switch_at)
    switch_end = _match_delimiter(method, switch_brace, "{", "}")
    assert switch_end > 0, (
        "the operation switch did not terminate; a truncated block yields an "
        "empty case set, which every check downstream reads as 'nothing registered'")
    return method[switch_brace:switch_end + 1]


def _rebase_switch_cases(source: str | None = None) -> dict[str, str]:
    """`case "op":` labels of the operation switch, mapped to their bodies.

    Three things this has to get right, each of which was wrong at some point:

    * **Stacked labels inherit.** `case "update_prompt_section": case
      "delete_prompt_section":` means both run the next body, and four real
      labels are written that way. Reading each label only up to the next one
      reports them empty and would demand a no-op entry for a registered rebase.
    * **A terminated no-op does not inherit.** A body of comments plus `break;`
      is a deliberate no-op, so it must not absorb the following case's work.
    * **Only labels of *this* switch count, and only live ones.** A nested
      `switch` inside a case body would otherwise inject phantom labels that
      truncate their parent, and a commented-out case block would be adopted as
      a live registration.
    """
    body = source if source is not None else _rebase_switch_block()
    mask = _code_mask(body)
    labels, depth = [], 0
    for index, char in enumerate(body):
        if not mask[index]:
            continue
        if char in "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif depth == 1:
            match = re.match(r'case\s+"([a-z_]+)"\s*:|default\s*:', body[index:])
            at_line_start = index == 0 or body[:index].rstrip(" \t").endswith(("\n", "{", ";"))
            if match and at_line_start:
                labels.append((match.group(1), index, index + match.end()))
    spans = []
    for order, (name, start, after) in enumerate(labels):
        stop = labels[order + 1][1] if order + 1 < len(labels) else len(body)
        spans.append((name, body[after:stop]))
    cases, inherited = {}, ""
    for name, own in reversed(spans):
        if _case_body_terminates(own):
            inherited = own
        if name is not None:
            cases[name] = inherited
    return cases


# `break`/`continue` with an optional label, `return`, or `throw`. Matched on
# code only: `break` inside a string or a comment does not end a case.
_TERMINATOR_RE = re.compile(r"\b(?:break|continue)\b\s*\w*\s*;?|\b(?:return|throw)\b")


def _case_body_terminates(body: str) -> bool:
    """True when control leaves the case rather than falling into the next label."""
    return bool(_TERMINATOR_RE.search(_code_only(body)))


def _case_body_is_effective(body: str) -> bool:
    """True when a case body does work, rather than only commenting and leaving.

    The terminator is a token set, not the literal `break;`. The switch sits
    inside `for (const operation of rebased.operations)`, so `continue;` is an
    idiomatic no-op there, and `break ;` / `break outer;` are equally empty --
    all three read as work if only `break;` is stripped.
    """
    return bool(re.sub(r"\s+|\{|\}", "", _TERMINATOR_RE.sub("", _code_only(body))))


# ---------------------------------------------------------------------------
# Scanner health — a scan that silently matches nothing is worse than none
# ---------------------------------------------------------------------------
def test_the_emitting_module_set_is_closed():
    """A new emitter must force a decision, not quietly fall outside the scan."""
    op_types = _dispatcher_op_types()
    emitting = set()
    for path in sorted(JS_DIR.glob("*.js")):
        source = path.read_text(encoding="utf-8")
        mask = _code_mask(source)
        for match in _OP_ANCHOR_RE.finditer(source):
            if match.group(1) in op_types and mask[source.index("{", match.start())]:
                emitting.add(path.name)
                break
    assert emitting == set(EMITTING_MODULES), (
        "the set of modules emitting scene-mutation operations changed. Every "
        "tripwire in Phases 2 and 3 scans only EMITTING_MODULES, so an unlisted "
        "emitter is invisible to all of them. Added: "
        f"{sorted(emitting - set(EMITTING_MODULES))}; "
        f"gone: {sorted(set(EMITTING_MODULES) - emitting)}")


def test_every_operation_literal_resolves_to_a_named_scope():
    """Orphans fail; they are never skipped.

    The scope resolver is what Phase 3's exemption keys are built from. A literal
    it cannot place would otherwise drop out of the scan entirely, which is the
    silent-pass mode this module exists to prevent.
    """
    literals = _operation_literals()
    assert literals, "the scan found no operation literals at all"
    orphans = [f"{item['module']}:{item['line']} {item['op_type']}"
               for item in literals if not item["scope"]]
    assert not orphans, (
        "these operation literals resolve to no named scope, so they cannot be "
        "keyed or exempted. If one sits in a comment the mask should have "
        "dropped it; otherwise widen _SCOPE_DECL_RE rather than skipping them: "
        + ", ".join(orphans))


def test_operation_literal_extents_are_bounded():
    """A runaway brace matcher finds `expected` everywhere and reports zero gaps."""
    literals = _operation_literals()
    unterminated = [f"{i['module']}:{i['line']}" for i in literals if not i["terminated"]]
    assert not unterminated, f"unterminated operation literals: {unterminated}"

    trim = [item for item in literals
            if item["scope"] == "_commitTrim" and item["op_type"] == "update_clip"]
    assert len(trim) == 1, "expected exactly one update_clip literal in _commitTrim"
    assert "validate_lane_collision" in trim[0]["extent"]
    assert "update_audio_track" not in trim[0]["extent"], (
        "the extent swallowed the sibling literal; every guard check downstream "
        "is meaningless once extents overlap")


def test_operation_literal_counts_are_pinned():
    counts = {}
    for item in _operation_literals():
        counts[item["module"]] = counts.get(item["module"], 0) + 1
    assert counts == EXPECTED_LITERAL_COUNTS, (
        "operation-literal counts moved. If you added or removed an emission, "
        "update EXPECTED_LITERAL_COUNTS deliberately. Current: "
        + ", ".join(f"{module}={counts.get(module, 0)}"
                    for module in sorted(set(counts) | set(EXPECTED_LITERAL_COUNTS))))


def test_no_operation_literal_extent_swallows_another():
    """What the count cannot see.

    The count is one per anchor, so it does not move when an extent runs past its
    own closing brace and absorbs the next literal — and a swallowed extent finds
    `expected` in its neighbour, reporting a guarded site where there is a gap.
    Only a runaway reaching end-of-file shows up as `terminated: False`. Stated as
    "no extent contains another's anchor" rather than as a length bound, because a
    bound wide enough to survive ordinary edits is too wide to catch one swallow.
    """
    for module in EMITTING_MODULES:
        literals = [item for item in _operation_literals() if item["module"] == module]
        for item in literals:
            start, stop = item["offset"], item["offset"] + len(item["extent"])
            contained = [other["line"] for other in literals
                         if other is not item and start < other["offset"] < stop]
            assert not contained, (
                f"{module}:{item['line']} {item['op_type']} spans the anchor of "
                f"the literal(s) at line(s) {contained}")


def test_every_scene_operation_is_reachable_through_the_anchor():
    """Catches a literal whose `type` is not its first key.

    Nothing else in this module would see such a literal: the anchor requires
    `type` first, so the operation would silently leave the scan.
    """
    op_types = _dispatcher_op_types()
    assert op_types, "no dispatcher op types; this check would pass vacuously"
    missed = []
    for module in EMITTING_MODULES:
        source = (JS_DIR / module).read_text(encoding="utf-8")
        mask = _code_mask(source)
        anchored = {match.start(1) for match in _OP_ANCHOR_RE.finditer(source)}
        for match in re.finditer(r'type:\s*"([a-z_]+)"', source):
            if (match.group(1) in op_types and match.start(1) not in anchored
                    and mask[match.start()]):
                missed.append(f"{module}:{source.count(chr(10), 0, match.start()) + 1} "
                              f"{match.group(1)}")
    assert not missed, (
        "these scene operations are declared with `type` after another key, so "
        "the `{ type:` anchor cannot see them: " + ", ".join(missed))


def test_no_recorded_scope_spans_cross():
    """Underwrites `_enclosing_scope`'s smallest-span-is-innermost assumption."""
    for module in EMITTING_MODULES:
        spans = _scopes((JS_DIR / module).read_text(encoding="utf-8"))
        ordered = sorted(spans, key=lambda item: item[1])
        for outer_index, (_, start, end) in enumerate(ordered):
            for _, other_start, other_end in ordered[outer_index + 1:]:
                if other_start > end:
                    break
                assert other_end <= end, (
                    f"{module}: scope spans partially overlap at {other_start}, "
                    "so the innermost owner is undefined")


def test_the_rebase_switch_reads_as_cases_with_bodies():
    """`_rebase_switch_cases` against the real file, which Phase 2 depends on."""
    cases = _rebase_switch_cases()
    assert len(cases) > 15, f"only {len(cases)} rebase cases found; the scan truncated"
    assert "update_clip" in cases
    assert "rebaseLaneIndex" in cases["update_clip"], (
        "a known-working case reads as having no body, so the empty-case check "
        "Phase 2 relies on would classify every case as deliberate no-op")


def test_scope_resolution_agrees_with_the_existing_method_regex():
    """Parity for a second authority on 'which method owns this offset'.

    `test_mutation_gesture_coverage.py` answers the same question by a different
    algorithm. Two algorithms over one source diverge when the source changes, not
    only when someone edits a copy, so the agreement is asserted rather than
    assumed.
    """
    source = WIDGET.read_text(encoding="utf-8")
    tail_at = source.index("export class EditorWidget {")
    by_regex = {}
    for match in re.finditer(r"^    (?:async )?(\w+)\(.*?^    \}\n",
                             source[tail_at:], re.M | re.S):
        by_regex[match.group(1)] = (tail_at + match.start(), tail_at + match.end())

    mismatches = []
    for item in _operation_literals():
        if item["module"] != "editor_widget.js":
            continue
        span = by_regex.get(item["scope"])
        if not span or not span[0] <= item["offset"] <= span[1]:
            mismatches.append(f"{item['line']} {item['op_type']} -> {item['scope']}")
    assert not mismatches, (
        "containment resolution and the method regex disagree on the owning "
        "method: " + ", ".join(mismatches))


# ---------------------------------------------------------------------------
# Guards the guard — every check above must fire on a reverted source
# ---------------------------------------------------------------------------
def test_the_predicate_catches_every_shape_a_guard_can_hide_in():
    """Each case below is a shape that defeated an earlier pass of this scan."""
    guarded = '{ type: "update_clip", clip_id: id, frame: 10, expected: { frame: 9 } }'
    unguarded = '{ type: "update_clip", clip_id: id, frame: 10 }'
    assert _carries_client_payload(unguarded) and not _is_guarded(unguarded)
    assert _carries_client_payload(guarded) and _is_guarded(guarded)

    # Shorthand geometry. `split_clip` passes `frame` this way; a colon-only
    # geometry pattern reported the Critical defect's own operation as carrying
    # no geometry at all.
    shorthand_geometry = '{ type: "split_clip", clip_id: id, frame, apply_linked: true }'
    assert _carries_client_payload(shorthand_geometry)
    assert not _is_guarded(shorthand_geometry)

    # Shorthand guard. The reference panel writes `expected,` and is the surface
    # documented as always carrying one; a colon-only guard pattern would have
    # exempted it permanently on the first seeding run.
    assert _is_guarded('{ type: "update_reference_item", id, expected, fields }')

    # `expected_prompt_template` is a template content-hash precondition, not a
    # claim about this row. Counting it certified a real unguarded write.
    assert not _is_guarded(
        '{ type: "update_scene_fields", fields, expected_prompt_template: template }')

    # Payloads under a key that is not `fields`, and not geometry at all.
    assert _carries_client_payload('{ type: "replace_prompt_sections", sections: next }')
    assert _carries_client_payload('{ type: "create_link_group", items: refs }')

    # Opaque payloads -- shorthand, spread, identifier -- are recorded as such
    # rather than skipped, so an opaque site stays distinguishable in the
    # catalogue from a transparent one whose children are pinned.
    for opaque in ('{ type: "update_clip", id, fields }',
                   '{ type: "update_clip", fields: { ...props } }',
                   '{ type: "update_clip", fields: body }'):
        assert "fields.<opaque>" in _payload_fingerprint(opaque), opaque
    transparent = _payload_fingerprint('{ type: "update_clip", fields: { frame: 3 } }')
    assert "fields.frame" in transparent and "fields.<opaque>" not in transparent
    # A nested guard copy must not decide the top-level classification.
    nested = _payload_fingerprint(
        '{ type: "update_clip", expected: { fields: prev }, fields: { frame: 1 } }')
    assert "fields.<opaque>" not in nested and "fields.frame" in nested


def test_a_brace_in_a_string_or_a_comment_does_not_truncate_the_keys():
    """Independently, because together they cancel.

    The earlier fixture carried both a `"}"` and a `/* { */`: the string closed
    the object and the comment reopened it, so the key after them was found by
    accident. Either half alone silently lost it.
    """
    assert _top_level_keys('{ type: "update_clip", label: "}", frame: 10 }') == (
        "frame", "label", "type")
    assert _top_level_keys('{ type: "update_clip", /* { */ frame: 10 }') == (
        "frame", "type")
    # A user-authored scene name may legally contain a brace.
    assert _top_level_keys(
        '{ type: "update_scene_fields", fields: { name: "Scene }" }, expected: { n: 1 } }'
    ) == ("expected", "fields", "type")


def test_the_fingerprint_records_keys_and_not_values():
    """A fingerprint that moves on a local rename trains people to bump it."""
    assert _top_level_keys(
        '{ type: "set_lane_count", lane_type: laneType, count: nextCount }'
    ) == ("count", "lane_type", "type")
    assert _top_level_keys(
        '{ type: "consolidate_items", remove_vacated_lanes: true }'
    ) == ("remove_vacated_lanes", "type")
    assert _top_level_keys(
        '{ type: "delete_prompt_section", index: idx, expected: undefined }'
    ) == ("expected", "index", "type")
    # It must still move when a key is added.
    assert (_top_level_keys('{ type: "set_lane_count", count: 2 }')
            != _top_level_keys('{ type: "set_lane_count", count: 2, expected: e }'))


def test_a_literal_inside_a_comment_is_not_an_emission():
    """The umbrella's Phase D adds a contract header to `editor_widget.js`.

    A documentation example there must not be adopted as a real emission, and
    must not be reported as an orphan either — both outcomes send the maintainer
    to the wrong repair.
    """
    block = (
        "/**\n"
        " * Example:\n"
        ' *     { type: "update_clip", clip_id, fields: { timeline_start_frame } }\n'
        " */\n"
        "function real() {\n"
        '    push({ type: "update_clip", clip_id: id, frame: 1 });\n'
        "}\n"
    )
    found = _scan_text(block)
    assert [(item["scope"], item["line"]) for item in found] == [("real", 6)]

    line_comment = (
        "function real() {\n"
        '    // { type: "update_clip", frame: 1 }\n'
        '    push({ type: "split_clip", clip_id: id, frame: 2 });\n'
        "}\n"
    )
    assert [item["op_type"] for item in _scan_text(line_comment)] == ["split_clip"]


def test_the_scope_resolver_reports_the_innermost_owner():
    """Proximity would report `inner`; containment must report `outer`."""
    source = (
        "async function outer(a, b = f(1)) {\n"
        "    const inner = (x) => { return x; };\n"
        '    push({ type: "update_clip", clip_id: id, frame: 1 });\n'
        "}\n"
    )
    assert [item["scope"] for item in _scan_text(source)] == ["outer"]


def test_the_scans_would_catch_a_regression():
    """Guards the guard for the module-level scans, with exact findings."""
    broken = _scan_text('function f() { push({ type: "update_clip", frame: 1 );\n')
    assert [item["terminated"] for item in broken] == [False]

    orphaned = _scan_text('export const ops = [{ type: "update_clip", frame: 1 }];\n')
    assert [item["scope"] for item in orphaned] == [""]

    cases = _rebase_switch_cases(
        "{\n"
        '    case "a":\n'
        "        // only a comment\n"
        "        break;\n"
        '    case "b":\n'
        "        rebase(operation);\n"
        "        break;\n"
        "}\n")
    assert set(cases) == {"a", "b"}
    assert "rebase(operation)" in cases["b"]
    assert "rebase(" not in cases["a"]


# ---------------------------------------------------------------------------
# Phase 2 — every scene operation has a declared history-rebase policy
# ---------------------------------------------------------------------------
# `_rebaseSceneMutationIntentForHistory` ends in `default: break`, so an operation
# with no case is retargeted by nothing and no one is told. When a history action
# is queued ahead of it, a lane index or a list position authored against the
# pre-history scene is sent as authored. The switch cannot be made exhaustive --
# most operations genuinely need no rebase -- so the policy is declared here
# instead, and an operation with no declaration fails.
#
# A fifth class is computed rather than listed: an operation the client never
# emits cannot reach a queued intent, so it needs no entry and gains one
# automatically the day someone emits it.

# Nothing here has an ADDRESS a history action can move, so there is nothing for
# a rebase to retarget. Two shapes qualify, and the second was added by umbrella
# Phase B rather than inherited:
#
#   1. Addressed by durable identity, or by project-level identity, carrying no
#      lane index and no list position.
#   2. Addressed by a RULER COORDINATE -- a frame, or a frame range -- or
#      carrying no address at all because it replaces a whole collection. A
#      coordinate still means the same coordinate after an Undo, so retargeting
#      it would move the mark the author placed rather than follow it. Scene
#      history does not retime: fps changes are outside it (durable_rules.md),
#      so nothing in a restore rescales a frame.
#
# Expiry: an entry leaves the day its operation gains a field whose meaning
# depends on the document it was read from -- a list position, a lane index, or a
# snapshot of a row's current values used as an address. A frame does not trigger
# this by itself; that is what clause 2 settles.
REBASE_EXEMPT = {
    "replace_clip_source": "Names a durable clip_id and an asset_id; no lane or "
                           "position to retarget.",
    "replace_audio_source": "Names a durable track_id and an asset_id.",
    "create_prompt_semantic_unit": "Mints project-level prompt identity; carries "
                                   "no scene geometry.",
    "import_prompt_context_dependencies": "Imports project-level profile and "
                                          "semantic-unit closures; no scene "
                                          "member is addressed.",
    # The three below were REBASE_UNREVIEWED until umbrella Phase B traced them.
    # All three turn on one distinction, which is why they resolve together: a
    # rebase retargets an ADDRESS that history moved. A frame is a coordinate the
    # author picked on the ruler, and it still means that coordinate after an
    # Undo, so there is nothing to follow.
    "create_guide": "Addressed by frame_index, a ruler coordinate. Its siblings "
                    "move_/update_/delete_guide rebase because they follow a "
                    "guide that MOVED; a create has no prior guide to follow, "
                    "and retargeting the frame would move the mark the author "
                    "placed. The replacement hazard that used to sit here is now "
                    "a guard rather than a rebase: umbrella Phase B gave the "
                    "operation `expected.replaces_guide_id`, so a queued Undo "
                    "that puts a different guide on that frame makes the create "
                    "REFUSE instead of destroying it. Refusing is the right "
                    "outcome for a coordinate whose contents moved -- the same "
                    "decision DELIBERATE_NO_PROJECTION records for the splits.",
    "create_prompt_section": "Addressed by a client-computed start/end frame "
                             "range, the same coordinate argument. A stale range "
                             "does not apply silently either: "
                             "_apply_create_prompt_section calls "
                             "_require_no_prompt_overlap, so a range history has "
                             "since occupied is refused rather than written. "
                             "Refusal is the right outcome for geometry drawn "
                             "against a layout that no longer exists -- the same "
                             "decision DELIBERATE_NO_PROJECTION records for the "
                             "splits.",
    "replace_prompt_sections": "Carries the whole collection, not an address. "
                               "There is no index or lane to retarget, and "
                               "projecting it forward would mean adopting the "
                               "ordered scene's sections -- replacing the "
                               "author's intent with the state they were "
                               "replacing. Rebase was never the tool for the "
                               "exposure here; umbrella Phase B gave the "
                               "operation `expected.sections` instead, so a "
                               "collection that moved refuses. Deliberately NOT "
                               "restated by a rebase, for the same reason as the "
                               "splits: it is the authored evidence of what was "
                               "being replaced.",
}

# Empty since umbrella Phase B traced its last five entries. It stays as a
# declared class rather than being deleted: `_rebase_policy_classes` needs
# somewhere to file an operation whose reason is honestly not yet known, and
# removing the bucket would leave "invent a reason for REBASE_EXEMPT" as the only
# way to satisfy the tripwire. Phase A wrote four invented reasons before that
# lesson was learned.
#
# Expiry: delete this dict only when the rebase policy stops being hand-declared.
REBASE_UNREVIEWED = {}

# Not projecting is correct here, and saying "defect" would contradict an
# approved plan. `_historyExpectedProjection` rewrites the `expected` keys that
# the ordered scene's row also carries, so projecting a split forward would make
# its own bounds guard compare current state against itself. (It rewrites only
# keys present on that row -- `resolved_end_frame` is computed and absent from
# `ReferenceItem.to_dict()`, so for the Reference split it is the authored
# `start_frame`/`end_frame` that projection would neuter.)
#
# Cross-plan coupling, recorded because it will fire: split-optimistic-local-apply.md
# L1 will add explicit empty `case "split_clip": case "split_audio_track":`
# labels. On that day these two move from "absent" to "empty case" and
# `test_rebase_policy_entries_are_not_stale` fails -- which is the correct prompt
# to re-read both documents, not a bug.
DELIBERATE_NO_PROJECTION = {
    "split_reference_item": "Documented on `case \"split_reference_item\"` in "
                            "`_rebaseSceneMutationIntentForHistory`: `frame` is a "
                            "position the author picked against a bar they could "
                            "see, and `expected.resolved_end_frame` exists so a "
                            "concurrent extent change refuses instead of cutting "
                            "a different bar at the same pixel. Projecting the "
                            "authored bounds forward would neuter that refusal.",
    "delete_prompt_semantic_unit_if_unreferenced": "Projection here would be a "
        "data-loss bug, not merely useless. The handler refuses unless "
        "`expected` matches the stored unit exactly, because -- its own words -- "
        "\"a partial projection would turn omitted fields into an authorization "
        "to delete later user edits\". That is why it is filed here and not as "
        "'nothing to retarget'.",
    "split_clip": "Same decision as split_reference_item; owned by "
                  "split-optimistic-local-apply.md L1, which adds the explicit "
                  "case and the `expected` bounds guard that makes "
                  "non-projection safe.",
    "split_audio_track": "As split_clip.",
}


def _rebase_policy_classes():
    """Every dispatcher op type mapped to the policy that covers it."""
    op_types = _dispatcher_op_types()
    cases = _rebase_switch_cases()
    emitted = {item["op_type"] for item in _operation_literals()}
    policies = {}
    for op_type in sorted(op_types):
        if op_type in DELIBERATE_NO_PROJECTION:
            policies[op_type] = "deliberate-no-projection"
        elif op_type in cases and _case_body_is_effective(cases[op_type]):
            policies[op_type] = "rebased"
        elif op_type not in emitted:
            policies[op_type] = "never-emitted"
        elif op_type in REBASE_EXEMPT:
            policies[op_type] = "exempt"
        elif op_type in REBASE_UNREVIEWED:
            policies[op_type] = "unreviewed"
        else:
            policies[op_type] = None
    return policies


def test_every_scene_operation_has_a_history_rebase_policy():
    """The tripwire: a new operation must declare one, or the suite fails."""
    policies = _rebase_policy_classes()
    undeclared = sorted(name for name, policy in policies.items() if policy is None)
    assert not undeclared, (
        "these scene operations reach `default: break` in "
        "`_rebaseSceneMutationIntentForHistory` with nothing recorded about why. "
        "Add a `case` if a queued history action can invalidate what they carry, "
        "or an entry to REBASE_EXEMPT / REBASE_UNREVIEWED in "
        f"tests/test_scene_mutation_registration.py with a reason: {undeclared}")


def test_a_case_that_does_nothing_is_declared_rather_than_merely_present():
    """An empty case is the silent default wearing a badge.

    Once this tripwire exists, the cheapest way to satisfy it is `case "x":
    break;` -- no reason, no review, and it reads as registration. `default:
    break` is at least honest about being a default, so an empty case must cost
    at least as much as an exemption.
    """
    cases = _rebase_switch_cases()
    undeclared = sorted(
        name for name, body in cases.items()
        if not _case_body_is_effective(body) and name not in DELIBERATE_NO_PROJECTION)
    assert not undeclared, (
        "these rebase cases are present but do nothing, which is registration in "
        "appearance only. Either give them a body or add them to "
        f"DELIBERATE_NO_PROJECTION with the reason they do nothing: {undeclared}")


def test_rebase_policy_entries_are_not_stale():
    """Exemptions must be able to fail, or they are documentation of gaps.

    `test_mutation_gesture_coverage.py`'s EXEMPT dict -- the model for this one --
    has no staleness check, so an entry outlives the handler it excused. This is
    the amendment that makes the ratchet enforcement.
    """
    op_types = _dispatcher_op_types()
    cases = _rebase_switch_cases()
    emitted = {item["op_type"] for item in _operation_literals()}

    for name, entries in (("REBASE_EXEMPT", REBASE_EXEMPT),
                          ("REBASE_UNREVIEWED", REBASE_UNREVIEWED),
                          ("DELIBERATE_NO_PROJECTION", DELIBERATE_NO_PROJECTION)):
        gone = sorted(set(entries) - op_types)
        assert not gone, f"{name} excuses operations the dispatcher no longer has: {gone}"

    overlap = set(REBASE_EXEMPT) & set(REBASE_UNREVIEWED)
    assert not overlap, f"an operation is both explained and unexplained: {sorted(overlap)}"

    # DELIBERATE_NO_PROJECTION is included deliberately. `_rebase_policy_classes`
    # tests it first, so an entry whose case later gains a real body would be
    # classified from the dict forever while the dict still says it does nothing.
    # That is the same hole this test exists to close for the other two.
    now_rebased = sorted(
        name for name in (set(REBASE_EXEMPT) | set(REBASE_UNREVIEWED)
                          | set(DELIBERATE_NO_PROJECTION))
        if name in cases and _case_body_is_effective(cases[name]))
    assert not now_rebased, (
        "these are filed as not rebasing -- exempt, unreviewed, or deliberately "
        "doing nothing -- but now have a case with a working body, so the entry "
        f"is stale and is shadowing what the code actually does: {now_rebased}")

    unemitted = sorted(name for name in set(REBASE_EXEMPT) | set(REBASE_UNREVIEWED)
                       if name not in emitted)
    assert not unemitted, (
        "these are exempted by hand but the client no longer emits them, so they "
        f"are covered by the computed never-emitted class instead: {unemitted}")

    # The durable property, stated so it survives the landing it anticipates:
    # a deliberate no-op is either absent from the switch or present with no
    # working body. `split-optimistic-local-apply.md` L1 moves `split_clip` and
    # `split_audio_track` from the first form to the second, which this accepts
    # without an edit -- while the combination that would actually be wrong
    # (filed as doing nothing, but doing something) is caught by `now_rebased`
    # above. An assertion pinning today's split between the two forms would only
    # be a notification, and its cheapest repair is to bump it.
    contradictory = sorted(
        name for name in DELIBERATE_NO_PROJECTION
        if name in cases and _case_body_is_effective(cases[name]))
    assert not contradictory, (
        "filed as deliberately doing nothing, but the case does work: "
        f"{contradictory}")


def test_the_rebase_policy_classes_cover_the_dispatcher_exactly():
    """Liveness: the classification must actually be reading the real surface."""
    policies = _rebase_policy_classes()
    assert len(policies) > 30, (
        f"the dispatcher exposes only {len(policies)} operations; the AST walk "
        "is reading a subset and every policy check above is weaker than it looks")
    counts = {}
    for policy in policies.values():
        counts[policy] = counts.get(policy, 0) + 1
    assert counts.get("rebased", 0) > 15, (
        f"only {counts.get('rebased', 0)} operations read as rebased (21 today); "
        "the case reader has probably stopped seeing bodies, which would let a "
        "real registration be re-filed as an exemption")


def test_the_rebase_policy_scan_would_catch_a_regression():
    """Guards the guard, with exact findings."""
    body_with_work = 'case "update_clip":\n    rebaseLaneIndex(operation);\n    break;\n'
    body_empty = 'case "update_clip":\n    // nothing to do\n    break;\n'
    assert _case_body_is_effective(_rebase_switch_cases("{\n" + body_with_work + "}")["update_clip"])
    assert not _case_body_is_effective(_rebase_switch_cases("{\n" + body_empty + "}")["update_clip"])

    # A stacked label inherits, and only until a terminator.
    stacked = _rebase_switch_cases(
        "{\n"
        '    case "a":\n'
        '    case "b":\n'
        "        work(b);\n"
        "        break;\n"
        '    case "c":\n'
        "        // deliberate no-op\n"
        "        break;\n"
        '    case "d":\n'
        "        work(d);\n"
        "        break;\n"
        "}\n")
    assert _case_body_is_effective(stacked["a"]) and "work(b)" in stacked["a"]
    assert _case_body_is_effective(stacked["b"])
    assert not _case_body_is_effective(stacked["c"]), (
        "a terminated no-op must not inherit the next case's body, or every "
        "deliberate no-op reads as registered")
    assert "work(d)" not in stacked["c"]

    # An empty case appended immediately before `default:` -- the one position a
    # developer naturally appends at. Bounding the last label at the end of the
    # method instead of the end of the switch handed it `default:`, the loop's
    # closing braces and `return rebased;` as its body, and it read as work.
    appended = _rebase_switch_cases(
        "{\n"
        '    case "update_clip":\n        work();\n        break;\n'
        '    case "ripple_delete":\n        break;\n'
        "    default:\n        break;\n"
        "}\n")
    assert not _case_body_is_effective(appended["ripple_delete"])
    assert "default" not in appended["ripple_delete"]

    # `continue;` is the idiomatic no-op here, because the switch sits inside
    # `for (const operation of rebased.operations)`. Stripping only the literal
    # `break;` read all three of these as work.
    for form in ("continue;", "break ;", "break outer;", "continue outer;"):
        assert not _case_body_is_effective(f"\n        {form}\n"), form

    # A commented-out case block is dead code, not a registration.
    commented = _rebase_switch_cases(
        "{\n"
        '    case "update_clip":\n        work();\n        break;\n'
        "    /*\n"
        '    case "ripple_delete":\n        rebaseLaneIndex(operation);\n'
        "        break;\n"
        "    */\n"
        "}\n")
    assert sorted(commented) == ["update_clip"]

    # A nested switch must not inject phantom labels that truncate their parent.
    nested = _rebase_switch_cases(
        "{\n"
        '    case "update_audio_track":\n'
        "        switch (operation.role) {\n"
        '        case "stem":\n            break;\n'
        "        }\n"
        "        rebaseLaneIndex(operation);\n        break;\n"
        '    case "update_clip":\n        work();\n        break;\n'
        "}\n")
    assert sorted(nested) == ["update_audio_track", "update_clip"]
    assert _case_body_is_effective(nested["update_audio_track"]), (
        "the parent case lost its body to a nested switch, and the failure "
        "message would tell the maintainer to file a working case as a no-op")


# ---------------------------------------------------------------------------
# Phase 3 — a client-computed payload carries an identity guard, or says why not
# ---------------------------------------------------------------------------
# An operation that names a row by durable id but carries geometry the client
# computed from what it could see is only correct while that view is fresh. With
# a ~2 s write floor it frequently is not, and with no `expected*` the server
# applies the stale value rather than refusing. That is the shape of the Critical
# split defect, and `split_clip` is in the seed list below.
#
# Creates are exempted by computation rather than by listing: there is no prior
# row for an `expected` to describe. They are not thereby safe -- a create's lane
# index can still be stale -- but that is the rebase question above, not this one.

# One reason per operation, because the reason is a property of what the
# operation carries, not of the call site. The sites dict below pins *which*
# emissions are covered and with what payload shape.
#
# Expiry: an entry leaves when umbrella Phase B either adds the guard or records
# why the operation cannot carry one. None of these says the emission is safe.
GUARD_EXEMPT_REASONS = {
    "update_clip": "Names a durable clip_id, so the row is never mis-targeted, "
                   "but carries timeline geometry with no prior-identity check "
                   "at all. split-optimistic-local-apply.md records this "
                   "asymmetry as the enabling condition for the tracked entry "
                   "about items sitting past the end of their own media, and "
                   "says retrofitting the guard is a larger change than split.",
    "update_audio_track": "As update_clip; the same plan names both.",

    "update_scene_fields": "Scene-level fields have no row to identify -- the "
                           "scene comes from the URL -- so what an `expected` "
                           "would protect is the field's prior value. Only four "
                           "of the sixteen fields it writes are compared at all "
                           "(GUARD_CONTRACTS), and none of these emissions names "
                           "one. Decided per site in "
                           "GUARD_SITE_DISPOSITIONS: five are whole-value sets "
                           "where last-write-wins is correct, and two carry a "
                           "stronger guard through `promptEditFields` that no "
                           "lexical scan can see.",
    "unlink_items": "Carries prompt and guide refs the server resolves by list "
                    "position and frame index (`_item_ref_from_selection`), so "
                    "the refs themselves can go stale. The half a queued history "
                    "action could move is closed: umbrella Phase B gave this "
                    "operation a rebase case sharing `rebaseBulkItem`. What is "
                    "left is a concurrent other writer, which only a server-side "
                    "guard would catch.",
    # The two sites differ materially, so the reason names both rather than
    # generalising from the worse one.
    "bulk_delete_items": "Two shapes. `_deleteItemsInLaneWithinGesture` carries "
                         "durable clip/audio ids and a boolean -- there is no "
                         "prior row state an `expected` would describe. "
                         "`_deleteSelectedItemsWithinGesture` passes `items` as "
                         "an identifier, and the array it names DOES carry "
                         "per-item `expected` that the server validates; the "
                         "scan cannot see through the identifier. The "
                         "`apply_linked` residual that used to sit here is "
                         "closed -- `_item_ref_from_selection` validates each "
                         "item as it resolves it -- so what remains is only the "
                         "scan's blindness to an identifier.",
    "consolidate_items": "Carries `target_lane` and durable `item_ids`. The lane "
                         "index is not unguarded in the sense that matters -- "
                         "`_rebaseSceneMutationIntentForHistory` retargets it "
                         "through `rebaseLaneIndex`, which the rebase policy "
                         "above records as `rebased`. What carries no guard is "
                         "the item id set, and those are durable.",
    "create_clip": "Additive: there is no prior row for an `expected` to "
                   "describe. Its `track_index` is the rebase question, not this "
                   "one, and `case \"create_clip\"` retargets it.",
    "create_audio_track": "As create_clip; its lane_index is likewise rebased.",
    "create_prompt_section": "Additive; carries client-computed start/end frames "
                             "with no prior row to compare them against, and "
                             "`_require_no_prompt_overlap` refuses a range that "
                             "has since been occupied rather than writing it. Its "
                             "rebase policy is now REBASE_EXEMPT, on the same "
                             "coordinate argument.",
    "create_prompt_semantic_unit": "Mints project-level prompt identity. No "
                                   "scene row exists yet or is named.",
    # The two creates that are NOT purely additive. A name-based exemption would
    # have hidden both, which is why there is no computed create class.
    "create_link_group": "NOT purely additive: its entire payload is references "
                         "to pre-existing rows. The client builds per-item "
                         "`expected` and `_item_ref_from_selection` discards it, "
                         "resolving prompt by list index and guide by frame "
                         "index. The queued-history half is closed -- it now has "
                         "a rebase case sharing `rebaseBulkItem` -- so what "
                         "remains is a concurrent other writer.",
    "import_prompt_context_dependencies": "Project-level profile and "
                                          "semantic-unit closures; no scene row "
                                          "is addressed, so there is nothing an "
                                          "`expected` would name.",
}

# Every unguarded emission, keyed by `module:scope:op_type`, with one fingerprint
# per emission. Two reasons it is a list of key paths rather than a count:
#
#   * A count cannot tell "one site was guarded and another added" from "nothing
#     changed", and the count test's own failure message routes the maintainer to
#     bumping the number -- which is the repair that destroys the signal. A list
#     moves on that swap even when the two shapes match.
#   * Depth matters. Most of the geometry here lives inside `fields`/`items`, so
#     a top-level-only fingerprint could not see `fields: { muted }` become
#     `fields: { muted, track_index, timeline_start_frame }` under an exemption
#     whose reason is about carrying geometry.
#
# `<opaque>` marks a payload the scan cannot see through (`fields`,
# `fields: body`, `fields: { ...props }`). It is recorded rather than skipped so
# an opaque site is distinguishable from a transparent one.
GUARD_EXEMPT_SITES = {
    "editor_prompt_panel.js:attachReference:update_scene_fields": [
        ('fields', 'fields.<opaque>', 'type'),
    ],
    "editor_prompt_panel.js:openProfileEditor:update_scene_fields": [
        ('fields', 'fields.prompt_context_profile_id', 'type'),
    ],
    "editor_prompt_panel.js:renderContextSettings:update_scene_fields": [
        ('fields', 'fields.prompt_context_profile_id', 'type'),
    ],
    "editor_widget.js:_applyPromptSetupWithinGesture:create_prompt_semantic_unit": [
        ('handle_suggestion', 'type', 'unit', 'unit.<opaque>'),
    ],
    "editor_widget.js:_applyPromptSetupWithinGesture:import_prompt_context_dependencies": [
        ('profiles', 'profiles.<opaque>', 'semantic_units', 'semantic_units.<opaque>', 'type'),
    ],
    "editor_widget.js:_commitItemMove:update_audio_track": [
        ('fields', 'fields.<opaque>', 'track_id', 'type'),
    ],
    "editor_widget.js:_commitItemMove:update_clip": [
        ('clip_id', 'fields', 'fields.<opaque>', 'type'),
    ],
    "editor_widget.js:_commitTrim:update_audio_track": [
        ('apply_linked', 'fields', 'fields.source_in_frame', 'fields.timeline_end_frame', 'fields.timeline_start_frame', 'track_id', 'type', 'validate_lane_collision'),
    ],
    "editor_widget.js:_commitTrim:update_clip": [
        ('apply_linked', 'clip_id', 'fields', 'fields.source_in_frame', 'fields.source_out_frame', 'fields.timeline_end_frame', 'fields.timeline_start_frame', 'type', 'validate_lane_collision'),
    ],
    "editor_widget.js:_consolidateSelectedItemsToLaneWithinGesture:consolidate_items": [
        ('item_ids', 'lane_type', 'remove_vacated_lanes', 'target_lane', 'type'),
    ],
    "editor_widget.js:_convertClipRoleWithinGesture:update_clip": [
        ('clip_id', 'fields', 'fields.<opaque>', 'type'),
    ],
    "editor_widget.js:_createLinkGroupFromSelectionWithinGesture:create_link_group": [
        ('items', 'items.<opaque>', 'type'),
    ],
    "editor_widget.js:_deleteItemsInLaneWithinGesture:bulk_delete_items": [
        ('items', 'items.<opaque>', 'preserve_lanes', 'type'),
    ],
    "editor_widget.js:_deleteSelectedItemsWithinGesture:bulk_delete_items": [
        ('apply_linked', 'items', 'items.<opaque>', 'type'),
    ],
    "editor_widget.js:_handleAssetDropWithinGesture:create_audio_track": [
        ('fields', 'fields.asset_id', 'fields.lane_index', 'fields.timeline_start_frame', 'type'),
    ],
    "editor_widget.js:_handleAssetDropWithinGesture:create_clip": [
        ('fields', 'fields.asset_id', 'fields.crop_position', 'fields.dual_drop', 'fields.fit_mode', 'fields.role', 'fields.strength', 'fields.timeline_start_frame', 'fields.track_index', 'type'),
        ('fields', 'fields.asset_id', 'fields.audio_lane_index', 'fields.crop_position', 'fields.dual_drop', 'fields.fit_mode', 'fields.link_video_audio', 'fields.timeline_start_frame', 'fields.track_index', 'type'),
    ],
    "editor_widget.js:_moveItemToFrameWithinGesture:update_audio_track": [
        ('apply_linked', 'fields', 'fields.timeline_start_frame', 'track_id', 'type'),
    ],
    "editor_widget.js:_moveItemToFrameWithinGesture:update_clip": [
        ('apply_linked', 'clip_id', 'fields', 'fields.timeline_start_frame', 'type'),
    ],
    "editor_widget.js:_moveItemToNewLaneWithinGesture:update_audio_track": [
        ('fields', 'fields.lane_index', 'track_id', 'type'),
    ],
    "editor_widget.js:_moveItemToNewLaneWithinGesture:update_clip": [
        ('clip_id', 'fields', 'fields.track_index', 'type'),
    ],
    "editor_widget.js:_muteOperationForItem:update_audio_track": [
        ('apply_linked', 'fields', 'fields.muted', 'track_id', 'type'),
    ],
    "editor_widget.js:_muteOperationForItem:update_clip": [
        ('apply_linked', 'clip_id', 'fields', 'fields.muted', 'type'),
    ],
    "editor_widget.js:_renameSceneWithinGesture:update_scene_fields": [
        ('fields', 'fields.name', 'type'),
    ],
    "editor_widget.js:_runRedoWithinGesture:create_prompt_semantic_unit": [
        ('handle_suggestion', 'type', 'unit', 'unit.<opaque>'),
    ],
    "editor_widget.js:_saveNewPromptSectionWithinGesture:create_prompt_section": [
        ('fields', 'fields.<opaque>', 'type'),
    ],

    "editor_widget.js:_toggleSelectedMuteWithinGesture:update_audio_track": [
        ('apply_linked', 'fields', 'fields.muted', 'track_id', 'type'),
    ],
    "editor_widget.js:_toggleSelectedMuteWithinGesture:update_clip": [
        ('apply_linked', 'clip_id', 'fields', 'fields.muted', 'type'),
    ],
    "editor_widget.js:_unlinkSelectedItemsWithinGesture:unlink_items": [
        ('entire_group', 'items', 'items.<opaque>', 'type'),
    ],
    "editor_widget.js:_updateItemPropertyWithinGesture:update_audio_track": [
        ('apply_linked', 'fields', 'fields.<opaque>', 'track_id', 'type'),
    ],
    "editor_widget.js:_updateItemPropertyWithinGesture:update_clip": [
        ('apply_linked', 'clip_id', 'fields', 'fields.<opaque>', 'type'),
    ],
    "editor_widget.js:_updateSceneDurationWithinGesture:update_scene_fields": [
        ('fields', 'fields.duration_frames', 'type'),
    ],
    "editor_widget.js:_updateSceneFpsWithinGesture:update_scene_fields": [
        ('fields', 'fields.fps', 'type'),
    ],
    "editor_widget.js:_updateSceneGlobalContextWithinGesture:update_scene_fields": [
        ('expected_prompt_template', 'fields', 'fields.<opaque>', 'type'),
    ],
    "editor_widget.js:_updateSceneResolutionWithinGesture:update_scene_fields": [
        ('fields', 'fields.height', 'fields.width', 'type'),
    ],
    "prompt_context_chips.js:configurePromptAttachment:create_prompt_semantic_unit": [
        ('handle_suggestion', 'type', 'unit', 'unit.definition', 'unit.kind', 'unit.name', 'unit.semantic_unit_id'),
    ],
}


def _unguarded_payload_sites():
    """Every emission carrying a client-computed payload with no identity guard."""
    sites = {}
    for item in _operation_literals():
        if not _carries_client_payload(item["extent"], item["mask"]):
            continue
        if _is_guarded(item["extent"], item["mask"]):
            continue
        key = f"{item['module']}:{item['scope']}:{item['op_type']}"
        sites.setdefault(key, []).append(
            (item["line"], _payload_fingerprint(item["extent"], item["mask"])))
    return sites


def test_every_unguarded_client_payload_is_accounted_for():
    """The tripwire: a new unguarded emission must be decided, not defaulted."""
    sites = _unguarded_payload_sites()
    assert sites, "the payload scan found nothing; it is not reading the tree"
    unaccounted = sorted(set(sites) - set(GUARD_EXEMPT_SITES))
    assert not unaccounted, (
        "these emissions carry a payload the client computed -- geometry, or a "
        "collection it assembled -- with no `expected*` guard, so the server "
        "applies whatever they say against whatever document it loads. Add an "
        "`expected` snapshot, or record the emission in GUARD_EXEMPT_SITES with "
        f"a reason in GUARD_EXEMPT_REASONS: {unaccounted}")


def test_unguarded_payload_shapes_have_not_drifted():
    """A fingerprint change means the payload changed under its exemption."""
    sites = _unguarded_payload_sites()
    drifted = []
    for key, expected in sorted(GUARD_EXEMPT_SITES.items()):
        found = [fingerprint for _line, fingerprint in sites.get(key, [])]
        if found and found != list(expected):
            drifted.append(f"{key}: {list(expected)} -> {found}")
    assert not drifted, (
        "the payload of an exempted emission changed while its exemption stayed, "
        "or a second unguarded emission joined it in the same method. The list "
        "is per emission, so a fix-one/add-one swap moves it even when the "
        "shapes match. Re-read the reason and decide whether it still holds: "
        + "; ".join(drifted))


def test_guard_exemptions_are_not_stale():
    """An exemption must be able to die, or it is documentation of a gap."""
    sites = _unguarded_payload_sites()
    gone = sorted(set(GUARD_EXEMPT_SITES) - set(sites))
    assert not gone, (
        "these exemptions no longer match any unguarded emission -- the site was "
        "guarded, renamed or removed -- so the entry is dead and should go with "
        f"it: {gone}")

    op_types = {key.rsplit(":", 1)[-1] for key in GUARD_EXEMPT_SITES}
    missing = sorted(op_types - set(GUARD_EXEMPT_REASONS))
    assert not missing, f"exempted with no reason recorded: {missing}"
    unused = sorted(set(GUARD_EXEMPT_REASONS) - op_types)
    assert not unused, f"reasons for operations no longer exempted: {unused}"


def test_the_guard_scan_sees_both_guarded_and_unguarded_emissions():
    """Liveness. `_is_guarded` returning True for everything empties the scan.

    Bounded against the total number of payload-carrying emissions, which does
    not shrink by design. An earlier version compared the unguarded count against
    the size of the exemption catalogue — a quantity Phase B exists to drain — so
    it would have started failing precisely as the debt was paid off, with a
    message blaming the wrong side.
    """
    carrying = [item for item in _operation_literals()
                if _carries_client_payload(item["extent"], item["mask"])]
    guarded = [item for item in carrying if _is_guarded(item["extent"], item["mask"])]
    assert len(carrying) > 40, (
        f"only {len(carrying)} emissions read as carrying a client payload; the "
        "predicate has probably stopped matching")
    assert guarded, (
        "no emission reads as guarded, so `_is_guarded` is matching nothing and "
        "every site would be catalogued as a gap")
    assert len(guarded) < len(carrying), (
        "every emission reads as guarded, so `_is_guarded` is matching "
        "everything and the catalogue would silently empty")


def test_the_guard_scan_would_catch_a_regression():
    """Guards the guard, with exact findings."""
    unguarded = _scan_text(
        'function gesture() { send({ type: "update_clip", clip_id: id, frame: 1 }); }\n')
    assert len(unguarded) == 1
    assert _carries_client_payload(unguarded[0]["extent"], unguarded[0]["mask"])
    assert not _is_guarded(unguarded[0]["extent"], unguarded[0]["mask"])

    guarded = _scan_text(
        'function gesture() { send({ type: "update_clip", clip_id: id, frame: 1,'
        ' expected: { frame: 0 } }); }\n')
    assert _is_guarded(guarded[0]["extent"], guarded[0]["mask"])

    # A create is exempt by computation, but only because of its name -- so the
    # predicate must still see its payload, or the exemption is hiding nothing.
    create = _scan_text(
        'function gesture() { send({ type: "create_clip", fields: { track_index: 1 } }); }\n')
    assert _carries_client_payload(create[0]["extent"], create[0]["mask"])
    assert not _is_guarded(create[0]["extent"], create[0]["mask"])


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Phase 4 -- a coalescing gesture whose payload cannot be replaced wholesale
#            supplies a merge, or says why not
# ---------------------------------------------------------------------------
# Coalescing with no `merge` replaces the older intent outright
# (`project_mutation_queue.js`: `existing.intent = intent`) and settles every
# collapsed waiter from the survivor's result, so the losing gesture believes it
# succeeded. That is harmless only when the payload is a whole-value set the
# newer intent fully subsumes.
#
# It is NOT harmless when the server applies the value key-by-key: an inner key
# the newer intent omits is then never restored from the older one, and the edit
# is lost. Each entry names the setter that proves the key is sub-keyed.
#
# `fields` is deliberately NOT in this list, and the distinction is the whole
# predicate. `_apply_scene_fields` merges at the *field* level -- it writes only
# the keys `fields` names -- but each named field's VALUE is replaced outright,
# so a newer intent naming the same field subsumes the older one. The keys below
# are the ones whose value is itself applied key by key. Add `fields` and all
# three live whole-value gestures trip for no reason.
#
# Expiry: an entry leaves when its setter stops applying the value key-by-key.
SUBKEYED_PAYLOAD_VALUES = {
    "global_channels": "`Scene.set_global_channels` starts from "
                       "`dict(self.global_channel_docs)` and writes only the keys "
                       "the patch names, so an omitted channel keeps its stored "
                       "value rather than the older intent's "
                       "(`server/timeline_state.py`).",
    "global_channel_docs": "`Scene.set_global_channel_documents` normalises over "
                           "`set(self.global_channels) | set(documents)`, and "
                           "`prompt_context.normalize_channel_documents` rebuilds "
                           "an omitted key from the stored flat mirror -- so an "
                           "omitted document is not merely kept, it is "
                           "reconstructed without its structure.",
    "channels": "`PromptSection.set_channels` is the section-level twin of "
                "`set_global_channels`; `_next_prompt_section_content` and "
                "`_apply_linked_bounds_update` both reach it.",
    "channel_docs": "`PromptSection.set_channel_documents` is the section-level "
                    "twin of `set_global_channel_documents`.",
    "prompt_edit": "`_merge_prompt_edit_fields` applies one change per document "
                   "and per attachment key, each behind its own `expected`. A "
                   "replaced intent drops the changes it does not name.",
}

# A coalescing site with no merge that the scan cannot clear on its own. Keyed
# `module:scope:op_type` with the payload fingerprint, never a line number: a
# line-keyed entry rots on an unrelated edit above it, and the staleness test
# would then report the site as fixed when nothing about it had changed.
#
# Seeded EMPTY on 2026-09-17: after the two dead patch-shaped gestures were
# deleted, every coalescing site either supplies an effective merge or sends a
# fully transparent whole-value payload. An entry here claims a site is safe
# although the scan cannot see why, and needs the same tracing as any other.
COALESCE_WITHOUT_MERGE_REVIEWED: dict = {}

# The forwarding helpers. They are not gestures: they receive `coalesce` and
# `merge` from their caller and pass both through, so classifying them would
# report the plumbing rather than any decision. `_runQueueMutation` is here for
# the same reason; queue mutations are a sibling dispatcher and out of scope.
_FORWARDING_SCOPES = frozenset({
    "_runSceneMutation", "_queueProjectMutation", "_runQueueMutation"})

# A receiver is required. `_queueProjectMutation({` -- the declaration itself --
# otherwise matches, and reads as a call site with no enclosing scope.
_ENQUEUE_RE = re.compile(r"\.\s*_(?:runSceneMutation|queueProjectMutation)\s*\(")
_ARROW_MERGE_RE = re.compile(r"\(\s*(\w+)\s*,\s*(\w+)\s*\)\s*=>")
_FUNCTION_MERGE_RE = re.compile(r"\bfunction\s*\w*\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)")
_IMPORT_RE = re.compile(r"\bimport\s*\{([^}]*)\}\s*from\s*[\"']([^\"']+)[\"']")


def _merge_is_effective(extent_code: str, scope_body: str,
                        module_source: str = "") -> bool:
    """Does this site pass a merge that can actually preserve the older intent?

    Lexical presence is not enough. `merge: (a, b) => b` is the silent default
    wearing a badge -- the same shape Phase A had to reject for `case "x":
    break;` -- and it is the cheapest possible repair when this tripwire fires.
    A merge is effective only if its body reads its FIRST parameter, which is
    the older intent (`project_mutation_queue.js`: `merge(existing.intent, intent)`).

    An unresolvable merge is NOT effective. Failing closed sends the site to the
    reviewed dict, where a human states why; failing open would let any spelling
    the matcher cannot read buy a pass.
    """
    inline = re.search(r"\bmerge\s*:\s*(.+)", extent_code, re.S)
    named = re.search(r"\bmerge\s*[,}]", extent_code)
    body = ""
    if inline:
        body = inline.group(1)
    elif named:
        # Shorthand `merge,` -- the definition is a `const merge = ...` in the
        # same method, which is how both real merge-bearing gestures spell it.
        definition = re.search(r"\bconst\s+merge\s*=\s*(.+)", scope_body, re.S)
        if not definition:
            return False
        body = definition.group(1)
    else:
        return False
    if _reads_its_older_argument(body):
        return True
    # A bare identifier: either a `const` in the same method or a name imported
    # from a sibling module. The SHARED merge is the second shape
    # (`scene_mutation_coalescing.js`), and without this the scanner would read
    # a gesture passing it as having no effective merge -- which is fail-closed
    # but reports the opposite of the truth, and whose cheapest repair is to
    # inline a second copy of the merge. That is how two sources of truth get
    # created, so the scanner follows the import instead.
    identifier = re.match(r"\s*([A-Za-z_$][\w$]*)\s*[,}\n]", body)
    if not identifier:
        return False
    name = identifier.group(1)
    local = re.search(rf"\b(?:const|let)\s+{re.escape(name)}\s*=\s*(.+)",
                      scope_body, re.S)
    if local and _reads_its_older_argument(local.group(1)):
        return True
    return _imported_merge_reads_its_older_argument(module_source, name)


def _reads_its_older_argument(text: str) -> bool:
    """Does this two-parameter definition reference its FIRST parameter?

    Covers both spellings a merge can take: an arrow and a declared function.
    `merge: (a, b) => b` is the silent default wearing a badge, and so is
    `function merge(a, b) { return b; }`.
    """
    matches = [found for found in
               (_ARROW_MERGE_RE.search(text), _FUNCTION_MERGE_RE.search(text))
               if found]
    if not matches:
        return False
    # The EARLIEST match only. Trying one pattern and then falling through to
    # the other over the same text let a badge merge -- `(a, b) => b` -- be
    # certified by an unrelated two-parameter `function (a, b)` further down the
    # extent. The merge is whatever comes first after `merge:`; anything later
    # belongs to something else.
    found = min(matches, key=lambda one: one.start())
    older = found.group(1)
    return re.search(rf"\b{re.escape(older)}\b", text[found.end():]) is not None


def _imported_merge_reads_its_older_argument(module_source: str, name: str) -> bool:
    """Follow `name` to its export and ask the same question of it.

    Bounded to the declaration's OWN parameter list and body, not to a text
    window. Two looser attempts failed in opposite directions and both are worth
    recording: capturing only what follows the name dropped the `function`
    keyword, so every imported merge read as ineffective; capturing to the next
    top-level `export` reached a non-exported helper below it, so a
    ONE-parameter export passed on that helper's two parameters.

    Returns False when the name is not imported, the module is outside
    `web/js`, the export cannot be found, or it does not take two parameters --
    every one of which fails closed at the caller.
    """
    if not module_source:
        return False
    for match in _IMPORT_RE.finditer(module_source):
        # `{ exported as local }`: look the definition up by its EXPORTED name
        # and match the call site by its LOCAL one. Using the local name for
        # both made every aliased import unresolvable, which fails closed but
        # reports the opposite of the truth.
        aliases = {}
        for part in match.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            head, _, tail = part.partition(" as ")
            aliases[(tail or head).strip()] = head.strip()
        if name not in aliases:
            continue
        exported_name = aliases[name]
        target = JS_DIR / Path(match.group(2)).name
        if not target.is_file():
            return False
        text = target.read_text(encoding="utf-8")
        mask = _code_mask(text)
        exported = re.search(
            rf"\bexport\s+(?:function|const|let)\s+{re.escape(exported_name)}\b", text)
        if not exported:
            return False
        open_paren = text.find("(", exported.end())
        if open_paren < 0:
            return False
        close_paren = _match_delimiter(text, open_paren, "(", ")", mask)
        if close_paren < 0:
            return False
        parameters = _parameter_names(text[open_paren + 1:close_paren])
        if len(parameters) != 2:
            return False
        body = _definition_body(text, close_paren, mask)
        if not body:
            return False
        return re.search(rf"\b{re.escape(parameters[0])}\b", body) is not None
    return False


def _parameter_names(inside: str) -> list[str]:
    """The declared names, one per top-level comma, defaults stripped.

    `re.findall(r"\\w+")` over the whole list was wrong in both directions:
    `(older, newer = null)` counted three "parameters" and the definition was
    rejected, and a destructured or typed list would count more still.
    """
    names, depth, current = [], 0, []
    for char in inside:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            names.append("".join(current))
            current = []
            continue
        current.append(char)
    names.append("".join(current))
    resolved = []
    for name in names:
        head = name.split("=")[0].strip()
        if not re.fullmatch(r"[A-Za-z_$][\w$]*", head):
            return []          # destructured or otherwise unreadable: fail closed
        resolved.append(head)
    return [name for name in resolved if name]


def _definition_body(text: str, close_paren: int, mask: bytearray) -> str:
    """The body belonging to THIS definition, block or concise.

    Searching forward for the next `{` was a fail-OPEN bug: a concise arrow
    (`(a, b) => b`) has no block, so the search landed on an unrelated block
    later in the module and the first-parameter check ran against a stranger's
    code. `merge: (a, b) => b` is precisely the silent default this tripwire
    exists to reject, so resolving it against someone else's body is the one
    outcome that must not happen.
    """
    cursor = close_paren + 1
    while cursor < len(text) and text[cursor] in " \t\r\n":
        cursor += 1
    if text.startswith("=>", cursor):
        cursor += 2
        while cursor < len(text) and text[cursor] in " \t\r\n":
            cursor += 1
    if cursor >= len(text):
        return ""
    if text[cursor] == "{":
        end = _match_delimiter(text, cursor, "{", "}", mask)
        return text[cursor:end + 1] if end > 0 else ""
    # A concise body runs to the statement terminator at depth zero.
    depth, start = 0, cursor
    while cursor < len(text):
        if mask[cursor]:
            char = text[cursor]
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            elif char == ";" and depth == 0:
                return text[start:cursor]
        cursor += 1
    return text[start:]


# A `coalesce` this scan cannot read. Shorthand (`coalesce,`), a bare variable
# or any computed expression all mean "whichever caller is calling decides", and
# the decision then lives in the callers rather than at the enqueue. Reading
# those as `true` was a hole: `_updateItemPropertyWithinGesture` passes shorthand
# and `editor_widget.js` calls it with `{ coalesce: false }` and a traced reason
# for a Reference strength edit, on the stable key `reference:${id}:field:strength`.
# That is a real refusal on a repeating key and neither tripwire could see it.
_COALESCE_LITERAL_RE = re.compile(r"\bcoalesce\s*:\s*(?:true|false)\b")
_COALESCE_PRESENT_RE = re.compile(r"\bcoalesce\s*(?::|,|\})")


def _coalesce_is_caller_supplied(extent_code: str) -> bool:
    """`coalesce` is stated but not as a literal, so its value is not here."""
    if not _COALESCE_PRESENT_RE.search(extent_code):
        return False
    return not _COALESCE_LITERAL_RE.search(extent_code)


def _scan_enqueue_sites(sources: tuple) -> tuple:
    """Every scene-mutation enqueue in `sources`, with the decision it makes.

    `sources` is ((module, text), ...) rather than a fixed module list so a
    fixture can drive the whole predicate end to end. A tripwire that has never
    been observed to produce a finding is the false-confidence failure this
    module exists to prevent.
    """
    sites = []
    for module, source in sources:
        mask = _code_mask(source)
        scopes = _scopes(source, mask)
        literals = _scan(source, module)
        by_scope: dict = {}
        for literal in literals:
            by_scope.setdefault(literal["scope"], []).append(literal)
        for match in _ENQUEUE_RE.finditer(source):
            if not mask[match.start()]:
                continue
            paren = source.index("(", match.start())
            end = _match_delimiter(source, paren, "(", ")", mask)
            if end <= 0:
                raise AssertionError(
                    f"{module}: unterminated enqueue call at offset {paren}")
            code = _code_only(source[paren:end + 1], mask[paren:end + 1])
            scope = _enclosing_scope(scopes, match.start())
            # Innermost span containing the call, by the same smallest-span rule
            # `_enclosing_scope` uses, so a shorthand `merge,` can be resolved
            # against the `const merge = ...` in the method that passes it.
            span = min((s for s in scopes if s[1] <= match.start() <= s[2]),
                       key=lambda s: s[2] - s[1], default=None)
            scope_body = (_code_only(source[span[1]:span[2]], mask[span[1]:span[2]])
                          if span else "")
            # Read from the RAW extent, not `code`: the key is a template
            # literal, which `_code_mask` blanks, so the masked copy sees an
            # empty string where every one of these sites states its key.
            key_shape, key_interpolations = _enqueue_key(
                source, mask, span, paren, end)
            sites.append({
                "module": module,
                "scope": scope,
                "line": source.count("\n", 0, match.start()) + 1,
                "key_shape": key_shape,
                "key_interpolations": key_interpolations,
                # Omission resolves to the queue's own default, `true`. An
                # explicit `false` is a refusal. Anything else -- shorthand, a
                # variable, a computed value -- is CALLER-SUPPLIED and cannot be
                # read here at all, which is a third answer and not a synonym
                # for either: `_updateItemPropertyWithinGesture` takes
                # `coalesce` in its options bag and passes it through, and one
                # live caller declines with a traced reason.
                "coalesces": not re.search(r"\bcoalesce\s*:\s*false\b", code),
                "coalesce_is_caller_supplied": _coalesce_is_caller_supplied(code),
                "effective_merge": _merge_is_effective(code, scope_body, source),
                "operands": tuple(by_scope.get(scope, ())),
            })
    return tuple(sites)


@functools.lru_cache(maxsize=1)
def _enqueue_call_sites() -> tuple:
    return _scan_enqueue_sites(tuple(
        (module, (JS_DIR / module).read_text(encoding="utf-8"))
        for module in EMITTING_MODULES))


def _payload_leaf_keys(literal: dict) -> tuple:
    """(leaf key names the scan can see, whether any of the payload is opaque)."""
    names, opaque = set(), False
    fingerprint = _payload_fingerprint(literal["extent"], literal["mask"])
    for path in fingerprint:
        leaf = path.split(".")[-1]
        if leaf == "<opaque>":
            opaque = True
        else:
            names.add(leaf)
    # A literal carrying nothing but `type` is a top-level spread
    # (`{ type: "x", ...build() }`) or an empty payload. Neither is evidence of
    # a whole-value set, and reading it as one would be an affirmative safety
    # claim the scan has not earned.
    if not names - {"type"}:
        opaque = True
    return names, opaque


def _coalescing_sites_needing_review(sites=None) -> dict:
    """Coalescing sites with no effective merge that the scan cannot clear."""
    findings = {}
    for site in (sites if sites is not None else _enqueue_call_sites()):
        if not site["coalesces"] or site["effective_merge"]:
            continue
        if site["scope"] in _FORWARDING_SCOPES:
            continue
        base = f"{site['module']}:{site['scope']}"
        if not site["operands"]:
            findings[f"{base}:<operands-built-elsewhere>"] = (
                "coalesces with no effective merge and builds its operations "
                "outside this scope, so the payload cannot be read here")
            continue
        for literal in site["operands"]:
            names, opaque = _payload_leaf_keys(literal)
            fingerprint = _payload_fingerprint(literal["extent"], literal["mask"])
            key = f"{base}:{literal['op_type']}"
            hit = sorted(names & set(SUBKEYED_PAYLOAD_VALUES))
            if hit:
                findings[key] = (
                    f"coalesces with no effective merge while sending {hit} on "
                    f"`{literal['op_type']}`, whose value the server applies key "
                    f"by key; payload {list(fingerprint)}")
            elif opaque:
                findings[key] = (
                    f"coalesces with no effective merge and `{literal['op_type']}` "
                    f"carries a payload the scan cannot read, so it cannot be "
                    f"shown to be a whole-value set; payload {list(fingerprint)}")
    return findings


def test_a_coalescing_gesture_declares_a_merge_or_sends_a_whole_value_payload():
    """The tripwire: wholesale intent replacement must be a decision, not a default."""
    findings = _coalescing_sites_needing_review()
    unreviewed = {key: why for key, why in findings.items()
                  if key not in COALESCE_WITHOUT_MERGE_REVIEWED}
    assert not unreviewed, (
        "coalescing replaces the older intent outright and settles every collapsed "
        "gesture from the survivor's result, so an intent the newer one does not "
        "fully subsume is lost while its author is told it succeeded. Supply a "
        "`merge` that reads its FIRST argument -- see `_updateItemPropertyWithinGesture`: "
        "union the fields newer-wins, keep the OLDEST `expected` -- or record the "
        "site in COALESCE_WITHOUT_MERGE_REVIEWED with a traced reason. Do NOT "
        "reach for `coalesce: false`: sonder_editor_bugs.md measures that at 24.2 s "
        "for six clicks and 460 s for one burst, and umbrella Phase C owns it. "
        "A local rollback in the same gesture needs `onSupersededByCoalescing` "
        "instead, or it restores a superseded sibling's optimistic state: "
        + "; ".join(f"{key} -- {why}" for key, why in sorted(unreviewed.items())))


def test_coalescing_review_entries_are_not_stale():
    """An entry must be able to die, or it is documentation of a gap."""
    findings = _coalescing_sites_needing_review()
    gone = sorted(set(COALESCE_WITHOUT_MERGE_REVIEWED) - set(findings))
    assert not gone, (
        "these sites no longer coalesce without an effective merge, so the entry "
        f"is dead and should go with it: {gone}")


def test_the_coalescing_scan_sees_the_decisions_it_classifies():
    """Liveness, pinned rather than bounded, per this module's convention."""
    sites = _enqueue_call_sites()
    assert len(sites) == EXPECTED_ENQUEUE_SITES, (
        f"enqueue call sites moved: {len(sites)} found, "
        f"{EXPECTED_ENQUEUE_SITES} pinned. Update deliberately when adding or "
        "removing a scene-mutation enqueue.")
    assert any(site["coalesces"] for site in sites), "no site reads as coalescing"
    assert any(not site["coalesces"] for site in sites), "no site reads as opted out"
    assert any(site["effective_merge"] for site in sites), (
        "no site reads as supplying an effective merge, so `_merge_is_effective` "
        "is rejecting the two real ones and every coalescing site would be a finding")
    assert all(site["scope"] for site in sites), (
        "an enqueue resolved to no enclosing scope, which means the receiver-anchored "
        "regex is matching a declaration again")
    # The three known whole-value gestures must reach the predicate and clear it,
    # or it is passing them for the wrong reason.
    reviewed = _coalescing_sites_needing_review()
    for scope in ("_updateSceneResolutionWithinGesture", "_renameSceneWithinGesture",
                  "_updateSceneDurationWithinGesture"):
        site = next(s for s in sites if s["scope"] == scope)
        assert site["coalesces"] and not site["effective_merge"], (
            f"{scope} no longer coalesces without a merge; this test's premise moved")
        assert not any(key.split(":")[1] == scope for key in reviewed), (
            f"{scope} sends a whole-value payload and must clear the tripwire")


def test_the_coalescing_tripwire_produces_the_findings_it_claims():
    """Drives the whole predicate over fixtures, with exact findings.

    Without this the tripwire has never been observed to fire at all: the real
    modules are clean by construction, so every assertion above is satisfied by
    a predicate that returns nothing for any input.
    """
    def findings(js):
        return _coalescing_sites_needing_review(
            _scan_enqueue_sites((("fixture.js", js),)))

    subkeyed = findings(
        'async function gesture() {\n'
        '  await host._runSceneMutation([{ type: "update_scene_fields",\n'
        '    fields: { global_channels: patch } }], { coalesce: true });\n}\n')
    assert list(subkeyed) == ["fixture.js:gesture:update_scene_fields"]
    assert "global_channels" in subkeyed["fixture.js:gesture:update_scene_fields"]

    opaque = findings(
        'async function gesture() {\n'
        '  await host._runSceneMutation([{ type: "update_scene_fields", fields }],\n'
        '    { coalesce: true });\n}\n')
    assert list(opaque) == ["fixture.js:gesture:update_scene_fields"]
    assert "cannot read" in opaque["fixture.js:gesture:update_scene_fields"]

    # A top-level spread leaves only `type` visible. It must not read as a
    # whole-value set, because the scan has seen no value at all.
    spread = findings(
        'async function gesture() {\n'
        '  await host._runSceneMutation([{ type: "update_scene_fields", ...build() }],\n'
        '    { coalesce: true });\n}\n')
    assert list(spread) == ["fixture.js:gesture:update_scene_fields"]

    whole = findings(
        'async function gesture() {\n'
        '  await host._runSceneMutation([{ type: "update_scene_fields",\n'
        '    fields: { width: w, height: h } }], { coalesce: true });\n}\n')
    assert whole == {}, whole

    opted_out = findings(
        'async function gesture() {\n'
        '  await host._runSceneMutation([{ type: "update_scene_fields",\n'
        '    fields: { global_channels: patch } }], { coalesce: false });\n}\n')
    assert opted_out == {}, opted_out

    # An identity merge is the cheapest repair when this fires, and it must not
    # work: it discards the older intent exactly as no merge at all does.
    noop_merge = findings(
        'async function gesture() {\n'
        '  await host._runSceneMutation([{ type: "update_scene_fields",\n'
        '    fields: { global_channels: patch } }],\n'
        '    { coalesce: true, merge: (older, next) => next });\n}\n')
    assert list(noop_merge) == ["fixture.js:gesture:update_scene_fields"], noop_merge

    real_merge = findings(
        'async function gesture() {\n'
        '  await host._runSceneMutation([{ type: "update_scene_fields",\n'
        '    fields: { global_channels: patch } }],\n'
        '    { coalesce: true, merge: (older, next) => ({ ...older, ...next }) });\n}\n')
    assert real_merge == {}, real_merge

    # Omitting `coalesce` is a yes: the queue defaults it to true.
    omitted = findings(
        'async function gesture() {\n'
        '  await host._runSceneMutation([{ type: "update_scene_fields",\n'
        '    fields: { global_channels: patch } }], { label: "x" });\n}\n')
    assert list(omitted) == ["fixture.js:gesture:update_scene_fields"]


def test_the_two_real_merges_are_read_as_effective():
    """`_merge_is_effective` must clear the gestures it was modelled on."""
    sites = {site["scope"]: site for site in _enqueue_call_sites()}
    for scope in ("_saveLaneConfigWithinGesture", "_updateItemPropertyWithinGesture"):
        assert sites[scope]["effective_merge"], (
            f"{scope} passes a `const merge` shorthand that preserves the older "
            "intent; reading it as ineffective would make the tripwire fire on "
            "the two sites it holds up as correct")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Phase 5 -- a guard the server cannot honour is not a guard
# ---------------------------------------------------------------------------
# Phase 4 above catches an emission that carries NO `expected`. This section
# catches the opposite failure, and it is the one that would quietly destroy
# Phase 3's signal: an emission that carries an `expected` the server reads and
# throws away. Probed against the real route handler at 8735aa1 -- a
# deliberately FALSE `expected` on `update_clip`, and on
# `update_scene_fields { width }`, both returned 200 and applied the write,
# while the same shape on `update_scene_fields { global_channels }` returned
# 409. **15** of the dispatcher's 38 branches ignore any `expected` sent, down
# from the 23 this comment recorded when the probe was run: umbrella Phase B
# closed most of the gap and Phase C stage 2 L1 closed the two media splits.
# Re-measure before citing it -- `_branches_reading_expected()` is the authority
# and the count has moved twice.
#
# So `GUARD_EXEMPT_SITES` can be drained without protecting anything: add
# `expected: { timeline_start_frame: x }` to an `update_clip` emission and the
# Phase 3 tripwire clears, because it matches a token on the client. The
# contracts below are what stop that.

_GUARD_NAME_RE = re.compile(r"\bexpected(?:_[ab])?\b")


def _object_literal_keys(text: str) -> tuple[str, ...]:
    """Depth-1 keys of the object literal beginning at `text[0] == '{'`.

    A depth-1 spread contributes `<opaque>` ALONGSIDE the visible keys, wherever
    it sits. An earlier version only recognised a leading spread, so
    `{ prompt: p, ...rest }` reported `prompt` and nothing else -- the readable
    half silently certifying the half that is not readable.
    """
    depth, index, keys = 0, 0, []
    while index < len(text):
        char = text[index]
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1:
            if text.startswith("...", index):
                keys.append("<opaque>")
                index += 3
                continue
            match = re.match(r"([A-Za-z_$][\w$]*)\s*([:,}])", text[index:])
            starts_token = index == 0 or not re.match(r"[\w$.]", text[index - 1])
            if match and starts_token:
                previous = text[:index].rstrip()
                # As `_payload_fingerprint`: skip a ternary's middle operand.
                if ((match.group(2) == ":" and not previous.endswith("?"))
                        or (previous and previous[-1] in "{,")):
                    keys.append(match.group(1))
                    # Land ON the matched delimiter, not past it. Without the
                    # `continue` the trailing `index += 1` steps over a closing
                    # `}` -- so shorthand as the last key (`{ members }`) left
                    # the walk inside a literal it had already exited, and it
                    # went on collecting keys from the rest of the emission as
                    # if they belonged to the guard. `_payload_fingerprint`
                    # always had the `continue`; this copy did not.
                    index += match.end() - 1
                    continue
        index += 1
    return tuple(sorted(set(keys)))


def _enclosing_payload_key(code: str, offset: int) -> str:
    """Innermost `PAYLOAD_KEYS` collection whose value encloses `offset`.

    Innermost by span width, not by declaration order: with a guard nested under
    `fields` inside `items[]`, order would name whichever key `PAYLOAD_KEYS`
    happens to list last. No live site nests a guard that way, which is exactly
    why the tie-break has to be written down rather than observed.
    """
    found, width = "", None
    for key in PAYLOAD_KEYS:
        for match in re.finditer(r"\b%s\s*:" % key, code):
            rest = code[match.end():]
            stripped = rest.lstrip()
            if not stripped or stripped[0] not in "{[":
                continue
            opener = stripped[0]
            begin = match.end() + (len(rest) - len(stripped))
            # `code` is already code-only, so every character counts; building
            # a fresh mask over it would re-read the blanked string quotes.
            end = _match_delimiter(code, begin, opener,
                                   "}" if opener == "{" else "]",
                                   bytearray([1] * len(code)))
            if end > 0 and begin < offset < end and (width is None or end - begin < width):
                found, width = key + ("[]" if opener == "[" else ""), end - begin
    return found


def _guard_key_paths(extent: str, mask: bytearray | None = None) -> tuple[tuple[str, str, str], ...]:
    """Every `expected*` in one emission, as `(container, guard, key)` triples.

    Deliberately depth-agnostic, unlike `_payload_fingerprint`. Four live sites
    wrap the guard in a conditional spread —
    `...(laneId ? { expected: { lane_id: laneId } } : {})` — which puts the key
    below the operation literal's top level, so a depth-1 walker reports them as
    carrying no guard at all. That is the one direction this extractor must not
    fail in: reading a guarded site as unguarded silently exempts it from the
    tripwire below. `container` names the collection the guard sits inside
    (`items[]` for `bulk_delete_items`) or is empty for an operation-level guard.

    A value that is not an inline object literal records `<opaque>` — ES6
    shorthand, an identifier, or a computed expression. Opaque is a named class
    with its own catalogue, never a pass.
    """
    code = _code_only(extent, mask)
    paths = []
    for match in _GUARD_NAME_RE.finditer(code):
        start, end = match.start(), match.end()
        if start and re.match(r"[\w$.]", code[start - 1]):
            continue  # `expected_type`, `expected_prompt_template`, `x.expected`
        container = _enclosing_payload_key(code, start)
        rest = code[end:]
        stripped = rest.lstrip()
        if stripped.startswith(":"):
            value = stripped[1:].lstrip()
            if value.startswith("{"):
                keys = _object_literal_keys(value)
                paths.extend((container, match.group(0), key)
                             for key in (keys or ("<opaque>",)))
            else:
                paths.append((container, match.group(0), "<opaque>"))
        elif stripped.startswith((",", "}")):
            paths.append((container, match.group(0), "<opaque>"))  # shorthand
    return tuple(sorted(set(paths)))


# What a branch does with an `expected*` it is sent. Five kinds, because a
# per-operation boolean cannot hold the three real partials: `update_scene_fields`
# validates four of the sixteen fields it writes, `update_lane_config` validates
# one key and only for lane families that have a durable id, and
# `bulk_delete_items` validates per item and only for three of its five item
# types. Each entry carries one traced line of evidence -- the validating call,
# or the absence of one. Phase A's corrections record why that is mandatory:
# "a reason is a claim about code and has to be traced like one".
_FIXED = "fixed"                    # compares a closed set of keys
_WRITTEN_FIELDS = "written_fields"  # compares exactly the keys also in `fields`
_WHOLE_RECORD = "whole_record"      # requires and compares the complete record
_PER_ITEM = "per_item"              # the guard lives inside a collection
_NOTHING = "nothing"                # reads no `expected` on any path

# `_validate_guide_identity` (routes.py:1121) and `_validate_prompt_identity`
# (:1137) each compare a closed set; the sets are transcribed from the `checks`
# dicts plus the explicit tail comparisons.
_GUIDE_KEYS = frozenset({"guide_id", "frame_index", "asset_id", "source",
                         "strength", "muted"})
_PROMPT_KEYS = frozenset({"prompt_id", "start_frame", "end_frame", "prompt",
                          "muted", "channels", "channel_docs", "attachments",
                          "global_channel_exceptions"})
# The two media-split guards. Much narrower than their rows, and the omission
# that matters is `timeline_end_frame`: the end is the field the author's own
# previous cut rewrites, so guarding it refuses roughly half of every rapid-cut
# burst. The full reasoning, and the probe that produced it, is beside
# `_validate_clip_identity` in routes.py. Nothing about the source media is
# guarded either, because an unrelated concurrent property edit must not refuse
# a valid cut. `test_the_transcribed_key_sets_match_what_routes_compares`
# re-reads both out of routes.py and
# `test_an_emission_sends_every_key_its_branch_requires` re-reads the MANDATORY
# set, so the client and the server cannot drift apart in either direction.
_CLIP_KEYS = frozenset({"clip_id", "timeline_start_frame", "track_index", "role"})
_AUDIO_KEYS = frozenset({"track_id", "timeline_start_frame", "lane_index"})

GUARD_CONTRACTS = {
    # -- validates a closed key set -------------------------------------------
    "update_scene_fields": (_FIXED, frozenset({
        "prompt", "global_channels", "global_channel_docs", "global_attachments"}),
        "`_validate_global_prompt_expectations` compares exactly "
        "`_DIRECT_GLOBAL_PROMPT_FIELDS`. The other twelve fields "
        "`_apply_scene_fields` writes -- name, duration_frames, width, height, "
        "fps, prompt_context_profile_id, prompt_context_profile_config, "
        "generation_params and the four `VARIABLE_LANE_DESCRIPTORS` count attrs "
        "-- are written with no comparison of any kind. "
        "`_require_prompt_mutation_contract` runs first but only checks that the "
        "keys are PRESENT, so it is not a second comparison."),
    "update_lane_config": (_FIXED, frozenset({"lane_id"}),
        "`_apply_lane_config`, guarded only when the lane descriptor has a "
        "`recipe_attr` and only on lane_id. The deliberate bootstrap tolerance "
        "-- expected stays optional -- is owned by durable_rules.md, not by "
        "this entry."),
    "move_lane": (_FIXED, frozenset({"from_lane_id", "to_lane_id"}),
        "`_move_media_lane`. Unlike `_apply_lane_config` it REQUIRES expected "
        "and refuses a blank stored id, because the lane index is the thing the "
        "operation changes."),
    "move_guide": (_FIXED, _GUIDE_KEYS | {"replaces_guide_id"},
        "Two claims in one `expected`, because the operation touches two frames. "
        "`_validate_guide_identity` checks the SOURCE guide on both the "
        "apply_linked path and `_apply_move_guide`; `_validate_guide_destination` "
        "checks what is about to be replaced at the DESTINATION, which "
        "`_apply_move_guide` deletes exactly as `_apply_create_guide` does. The "
        "source validator compares a closed key set and ignores "
        "`replaces_guide_id`, so the two cannot collide. The guide being moved is "
        "excluded from the destination check: a drag that ends where it began "
        "legitimately finds itself there."),
    "update_guide": (_FIXED, _GUIDE_KEYS,
        "`_validate_guide_identity`, on both the apply_linked path and "
        "`_apply_update_guide`."),
    "delete_guide": (_FIXED, _GUIDE_KEYS,
        "`_validate_guide_identity`, on both the apply_linked path and "
        "`_apply_delete_guide`."),
    "update_prompt_section": (_FIXED, _PROMPT_KEYS,
        "`_validate_prompt_identity`, on both the apply_linked path and "
        "`_apply_update_prompt_section`, plus a batch pre-pass in "
        "`_apply_scene_mutations_sync` that validates every member before the "
        "first write. `subject_ids` is deliberately ignored rather than "
        "refused."),
    "delete_prompt_section": (_FIXED, _PROMPT_KEYS,
        "`_validate_prompt_identity`, on both the apply_linked path and "
        "`_apply_delete_prompt_section`."),
    "split_prompt_section": (_FIXED, _PROMPT_KEYS,
        "`_validate_prompt_identity`, called in the branch before "
        "`_apply_split_linked`."),
    "split_clip": (_FIXED, _CLIP_KEYS,
        "`_validate_clip_identity`, called in the dispatch branch on the clip "
        "`_find_clip` just resolved, with `_require_expected` making all five "
        "keys mandatory. The anchor ref handed to `_apply_split_linked` is "
        "built from that same validated object, so the guard and the split's "
        "own resolution cannot name different clips. The branch is also where "
        "the requirement lives rather than inside the validator, which keeps "
        "the legacy REST split route -- no `expected` at all -- working."),
    "split_audio_track": (_FIXED, _AUDIO_KEYS,
        "`_validate_audio_identity`, the same shape as `split_clip`."),
    "swap_prompt_sections": (_FIXED, _PROMPT_KEYS,
        "`_apply_swap_prompt_sections` calls `_validate_prompt_identity` twice, "
        "under `expected_a` and `expected_b` rather than `expected`."),
    "create_reference_item": (_FIXED, frozenset({"next_start_frame"}),
        "The dispatch branch calls `_validate_reference_creation_identity`, which "
        "recomputes `_next_reference_start_after` and compares it with what the "
        "caller measured its `end_frame` against. Additive in name only: both "
        "emitters read the next item's start out of their own copy of the lane. "
        "Two-sided, despite an earlier claim that `_require_no_reference_overlap` "
        "owned one direction -- a later item moved EARLIER does not always "
        "overlap the new extent, and then nothing else refuses it. A concurrent "
        "scene-duration shrink is the case still uncovered."),
    "replace_prompt_sections": (_FIXED, frozenset({"sections"}),
        "The dispatch branch calls `_validate_prompt_replacement_identity`, "
        "which compares the ordered `_prompt_section_structure` -- prompt ids "
        "with their bounds -- against what the caller believes it is replacing. "
        "The operation assigns `scene.prompt_sections` outright, so without this "
        "every concurrent edit in the write window went silently. A content edit "
        "that leaves ids and bounds alone is deliberately not covered; the "
        "validator says why and the bug tracker carries the remainder."),
    "create_guide": (_FIXED, frozenset({"replaces_guide_id"}),
        "The dispatch branch calls `_validate_guide_creation_identity` before "
        "`_apply_create_guide`; it delegates to `_validate_guide_destination`, "
        "shared with `move_guide`, which compares the caller's claim against "
        "every guide actually at that frame. `_apply_create_guide` REPLACES -- it "
        "drops every guide already there and rewrites their link groups through "
        "`_rewrite_link_groups_for_deleted` -- and the client mirrors that in "
        "`_applyLocalCreateGuide`, so replacement is deliberate. The guard names "
        "what is being replaced rather than requiring an empty frame, which lets "
        "the deliberate case through and refuses the blind one. Scoped to the "
        "dispatcher: `api_add_guide` still calls `_apply_create_guide` "
        "unguarded, and has no caller in this repository."),
    "remove_lane": (_FIXED, frozenset({"lane_count", "config", "lane_id"}),
        "`_remove_media_lane` calls `_validate_lane_removal_identity`, which "
        "requires `expected` and compares three things. `config` is the lane's "
        "normalized settings through `_normalized_lane_config`, the identity "
        "durable_rules.md already names for families with no durable id and the "
        "one `findLaneByAnchor` resolves by on the client; it is what catches a "
        "permutation that leaves the count unchanged. `lane_count` is compared "
        "as a FLOOR -- a family that only grew cannot have moved the caller's "
        "lane, because `_set_scene_lane_count` appends -- so demanding equality "
        "would refuse a concurrent append in which nothing moved. `lane_id` "
        "settles `reference`, the only movable family, and a blank stored id is "
        "tolerated as `_apply_lane_config` tolerates it. Known residual, stated "
        "in the validator: two non-reference lanes with identical configs are "
        "indistinguishable, which only a document version precondition closes."),
    "split_reference_item": (_FIXED, frozenset({
        "reference_item_id", "start_frame", "end_frame", "resolved_end_frame"}),
        "`_apply_split_reference_item` calls `_require_expected` with those four "
        "required and then compares each. `resolved_end_frame` is in the set "
        "because under end_frame == -1 the real bound comes from "
        "scene.duration_frames, which no other key names."),

    # -- validates whatever `fields` names -------------------------------------
    "update_reference_item": (_WRITTEN_FIELDS, frozenset(),
        "`_apply_update_reference_item` calls `_reference_item_expected` with "
        "`fields.keys()`, and that helper iterates the keys it was given rather "
        "than the keys `expected` carries. `_require_expected` asserts only that "
        "those keys are a SUBSET of expected, so an extra key naming no written "
        "field is neither required nor compared -- it is ignored outright."),

    # -- requires the complete record ------------------------------------------
    "delete_reference_item": (_WHOLE_RECORD, frozenset(),
        "`_apply_delete_reference_item` calls `_reference_item_expected` with "
        "`set(item.to_dict())`, so every key of the stored item is both required "
        "and compared."),
    "delete_prompt_semantic_unit_if_unreferenced": (_WHOLE_RECORD, frozenset(),
        "`_apply_delete_prompt_semantic_unit_if_unreferenced` compares the whole "
        "dict. A partial projection would authorize deleting later user edits, "
        "which is why DELIBERATE_NO_PROJECTION holds it too."),

    # -- validates inside a collection -----------------------------------------
    "create_link_group": (_PER_ITEM, frozenset(),
        "`_add_link_group` resolves each member through "
        "`_item_ref_from_selection`, which validates that member's own "
        "`expected` at the point it resolves the row. The client has always "
        "built those snapshots in `_mutationItemFromSelection`; until umbrella "
        "Phase B nothing read them."),
    "unlink_items": (_PER_ITEM, frozenset(),
        "`_item_ref_from_selection` per item, validated as each row is "
        "resolved; `_unlink_refs` then acts on what it returned."),
    "bulk_delete_items": (_PER_ITEM, frozenset(),
        "`_apply_bulk_delete_items` reads `items[].expected` and validates guide "
        "through `_validate_guide_identity`, prompt through "
        "`_validate_prompt_identity` and reference through "
        "`_reference_item_expected`. The apply_linked path used to discard all of "
        "that -- every non-reference item went through "
        "`_item_ref_from_selection`, which read no expected -- so a guarded "
        "delete became an unguarded one purely by being linked. Umbrella Phase B "
        "put the check INSIDE that resolver, at each point a row is resolved: an "
        "earlier version validated in a separate pass that re-derived the "
        "resolution beside it, and the two could name different rows, which is "
        "worse than no guard. Clip and audio items carry no expected and resolve "
        "by durable id, so there is nothing to compare for them. An "
        "operation-LEVEL expected is still discarded outright: nothing outside "
        "the items loop reads one."),
}

# The 23 that read no `expected` on any path. Held as data rather than as
# "everything else" so `test_the_guard_contracts_still_match_the_code` can fail
# in BOTH directions -- an operation that gains validation, and one that loses it.
#
# Each names the handler its dispatch branch calls, verified by AST against
# routes.py rather than typed. An earlier pass of this dict cited line numbers
# and sixteen of them landed on the PREVIOUS branch's tail -- the reason
# `test_the_guard_evidence_cites_code_that_exists` now checks every name.
GUARD_CONTRACTS.update({op: (_NOTHING, frozenset(), evidence) for op, evidence in {
    "consolidate_items": "`_consolidate_media_items` reads target_lane and "
                         "item_ids, range-checks the lane and refuses an id it "
                         "cannot resolve; it reads no expected.",
    "create_audio_track": "`_apply_create_audio_track` reads fields only.",
    "create_clip": "`_apply_create_clip` reads fields only.",
    "create_prompt_section": "`_apply_create_prompt_section` reads fields only. "
                             "`_require_no_prompt_overlap` is a range check "
                             "against the other sections, not a claim about a "
                             "prior row.",
    "create_prompt_semantic_unit": "`_apply_create_prompt_semantic_unit` mints "
                                   "project-level identity and reads no "
                                   "expected, unlike its delete sibling.",
    "delete_audio_track": "`_delete_audio_track` and `_apply_delete_link_refs` "
                          "are addressed by durable track_id; nothing compared.",
    "delete_clip": "`_delete_clip` and `_apply_delete_link_refs` are addressed "
                   "by durable clip_id; nothing compared.",
    "delete_link_group": "The only branch with no handler call: "
                         "`_apply_scene_mutation_operation` filters "
                         "`scene.linked_item_groups` by group_id inline. "
                         "Nothing compared.",
    "import_prompt_context_dependencies": "`_apply_prompt_context_dependencies` "
                                          "extends project-level lists; no scene "
                                          "row is addressed.",
    "replace_audio_source": "`_apply_replace_audio_source`. Its `expected_type` "
                            "parameter names an ASSET TYPE and is not a row "
                            "guard -- the decoy a widened pattern certified "
                            "twice.",
    "replace_clip_source": "`_apply_replace_clip_source`; the same "
                           "`expected_type` decoy.",
    "set_lane_count": "`_set_scene_lane_count` takes a count; there is no row to "
                      "identify.",

    "update_audio_track": "`_apply_update_audio_track` on the plain path and "
                          "`_apply_linked_bounds_update` on the linked one. "
                          "Neither reads expected.",
    "update_clip": "`_apply_update_clip` on the plain path and "
                   "`_apply_linked_bounds_update` on the linked one. Neither "
                   "reads expected: the guard gap under the tracked entry about "
                   "items sitting past the end of their own media is here, not "
                   "on the client.",
    "update_lane_configs": "`_apply_lane_configs`, the legacy positional "
                           "whole-array replacement; nothing compared.",
}.items()})


# Guard values the scan cannot read through. Recorded per site, never treated as
# green: an opaque guard is exactly where a discarded one would hide, and the
# plan's own warning is that "treating opaque as green is a false-confidence
# test". Each entry says what makes it opaque and where the real keys are built.
OPAQUE_GUARD_SITES = {
    "editor_reference_panel.js:renderItems:delete_reference_item":
        "`expected` is an identifier holding the item snapshot. The contract is "
        "_WHOLE_RECORD, which requires every key of item.to_dict(), so a lexical "
        "key list could not certify it either way.",
    "editor_reference_panel.js:writeItem:update_reference_item":
        "`expected` is built above the literal from the prior row.",
    "editor_widget.js:_applyPromptSetupWithinGesture:update_scene_fields":
        "`expected: expectedSceneFields`, an identifier accumulated field by "
        "field above the literal, inside a conditional spread.",
    "editor_widget.js:_commitItemMove:swap_prompt_sections":
        "`expected_a` is an inline literal and reads fine; `expected_b` is an "
        "identifier. Half-opaque, and listed because the half that is readable "
        "must not certify the half that is not.",
    "editor_widget.js:_deletePromptSectionWithinGesture:delete_prompt_section":
        "`expected` is an identifier built from the section being deleted.",
    "editor_widget.js:_updateItemPropertyWithinGesture:update_guide":
        "`expected` is a computed Object.fromEntries over the changed keys.",
    "editor_widget.js:_updateItemPropertyWithinGesture:update_reference_item":
        "As above; the same computed guard serves both operations.",
    "editor_widget.js:_updatePromptSectionWithinGesture:update_prompt_section":
        "ES6 shorthand `expected,` -- the shape Phase A's predicate was corrected "
        "twice to see at all.",
    **{site:
       "`expected` is `_referenceCreationGuard(...)` on the host, which BOTH "
       "surfaces call -- the timeline drop and the Reference panel's Add -- and "
       "which each then reads `end_frame` back off, so the guard cannot describe "
       "a different measurement than the payload was built from. It landed as a "
       "method for that reason and the panel grew a second copy anyway; an audit "
       "caught it."
       for site in (
           "editor_widget.js:_placeReferencePayloadWithinGesture:create_reference_item",
           "editor_reference_panel.js:createItem:create_reference_item",
       )},
    "editor_widget.js:_applyPromptSetupWithinGesture:replace_prompt_sections":
        "`expected` is `_promptSectionsReplacementGuard()`, which must be read "
        "before anything in the batch touches the scene -- it names the "
        "collection about to be replaced, not the one replacing it.",
    **{"editor_widget.js:%s:move_guide" % scope:
       "`expected` spreads `_guideReplacementGuard(...)` over its inline source "
       "identity, so the destination half is not lexical. The source half IS "
       "readable and correct; listing the site keeps the readable half from "
       "certifying the half that is not."
       for scope in ("_commitItemMove", "_moveGuideToFrameWithinGesture")},
    **{"editor_widget.js:%s:create_guide" % scope:
       "`expected` is `_guideReplacementGuard(...)`, one builder shared by both "
       "guide-creating gestures. It must be read BEFORE the local apply, which "
       "removes the occupant it names -- which is exactly why it is a method and "
       "not an inline literal at two call sites."
       for scope in ("_addClipFrameToGuidesWithinGesture",
                     "_handleAssetDropWithinGesture")},
    **{"editor_widget.js:%s:remove_lane" % scope:
       "`expected` is `_laneRemovalGuard(...)`, one builder shared by all four "
       "removal gestures so the recipe lookup cannot drift between them. "
       "`test_the_lane_removal_guard_matches_its_contract` runs the builder and "
       "checks its keys against GUARD_CONTRACTS, which is a stronger check than "
       "reading the literal would have been."
       for scope in ("_deleteSelectedLanesAndItemsWithinGesture",
                     "_removeLaneDeletingItemsWithinGesture",
                     "_removeLaneWithItemsWithinGesture",
                     "_removeLaneWithinGesture")},
    "prompt_identity_transactions.js:promptIdentityCleanupPlan:"
    "delete_prompt_semantic_unit_if_unreferenced":
        "`expected` is the complete created unit. _WHOLE_RECORD again.",
}

# An emission sends an `expected` key the server never compares, and the key is
# provably inert. Empty, and meant to stay that way.
#
# It held one entry for the length of an audit. `_appendReferenceMembersWithinGesture`
# sent `expected: { members, reference_item_id }` against `fields: { members }`,
# so `_reference_item_expected` compared `members` alone. Exempting it was the
# wrong call: the tripwire's own message says to remove the key unless it is
# provably inert, the emission was already addressed by its top-level
# `reference_item_id`, and deleting one line was both cheaper than the exemption
# and the repair that keeps the signal. An exemption dict nobody can empty is the
# thing this phase exists to drain.
#
# Expiry: an entry leaves when the key is removed from the emission, or when the
# branch starts comparing it. Prefer removing the key.
DISCARDED_GUARD_KEYS = {}


@functools.lru_cache(maxsize=1)
def _routes_identifiers() -> frozenset[str]:
    """Every function, class and module-level constant routes.py defines."""
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        names.update(target.id for target in targets if isinstance(target, ast.Name))
    return frozenset(names)


@functools.lru_cache(maxsize=1)
def _client_identifiers() -> frozenset[str]:
    """Named scopes in the emitting modules, via the scanner's own resolver.

    A guard trace legitimately crosses the boundary -- `create_guide`'s reason
    only makes sense once you know `_applyLocalCreateGuide` mirrors the server's
    replace-at-frame -- so the citation check has to know both vocabularies or it
    would push a true cross-language fact out of the record.
    """
    names = set()
    for module in EMITTING_MODULES:
        source = (JS_DIR / module).read_text(encoding="utf-8")
        names.update(name for name, _start, _end in _scopes(source))
    return frozenset(names)


_CITATION_RE = re.compile(r"`([A-Za-z_][\w.]*)`")


def _cited_names(evidence: str) -> tuple[str, ...]:
    """Backticked identifiers in a piece of evidence, bare of any subscript."""
    return tuple(name.split("[")[0].rstrip(".")
                 for name in _CITATION_RE.findall(evidence))


def _validator_key_set(function_name: str) -> frozenset[str]:
    """The keys a `_validate_*_identity` helper compares, read out of routes.py.

    The helpers share one shape: a `checks = {...}` dict whose keys are compared
    in a loop, then zero or more `if "<key>" in expected:` tails for the values
    that need normalizing first. Reading both is what makes the frozensets above
    a transcription that can be CHECKED rather than one that has to be trusted.
    """
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == function_name):
            continue
        keys = set()
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Dict)
                    and any(isinstance(target, ast.Name) and target.id == "checks"
                            for target in sub.targets)):
                keys.update(key.value for key in sub.value.keys
                            if isinstance(key, ast.Constant))
            # `if "attachments" in expected:` -- a tail comparison.
            if (isinstance(sub, ast.Compare) and len(sub.ops) == 1
                    and isinstance(sub.ops[0], ast.In)
                    and isinstance(sub.left, ast.Constant)
                    and isinstance(sub.left.value, str)
                    and isinstance(sub.comparators[0], ast.Name)
                    and _GUARD_NAME_RE.fullmatch(sub.comparators[0].id)):
                keys.add(sub.left.value)
        assert keys, f"{function_name}: no compared keys found; the walk is blind"
        return frozenset(keys)
    raise AssertionError(f"{function_name} not found in routes.py")


@functools.lru_cache(maxsize=1)
def _direct_global_prompt_fields() -> frozenset[str]:
    """`_DIRECT_GLOBAL_PROMPT_FIELDS`, read out of routes.py rather than copied."""
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name)
                and target.id == "_DIRECT_GLOBAL_PROMPT_FIELDS"
                for target in node.targets)):
            continue
        call = node.value
        assert isinstance(call, ast.Call), "the constant is no longer a frozenset(...)"
        values = call.args[0]
        return frozenset(element.value for element in values.elts
                         if isinstance(element, ast.Constant))
    raise AssertionError("_DIRECT_GLOBAL_PROMPT_FIELDS not found in routes.py")


def _hands_on_a_client_guard(function, call: ast.Call) -> bool:
    """Does this call hand `function`'s `expected*` parameter a CLIENT guard?

    A function with no such parameter returns True, so the ordinary descent is
    unaffected -- the helper may read the operation dict itself, which is how
    `_apply_lane_config` honours one.

    Where the parameter exists, the value decides. `_remove_media_lane` is
    reached two ways: the dispatcher passes `op.get("expected")`, and
    `_consolidate_media_items` passes `LANE_INDEX_FROM_SERVER_STATE` because it
    is vacating a lane it has just emptied from state it already holds. Counting
    the second would report the whole `consolidate_items` branch as honouring a
    guard it never reads from the request -- and worse, would make the contract
    say so while the emissions stayed in the exemption catalogue.
    """
    arguments = function.args
    names = [argument.arg for argument in
             arguments.posonlyargs + arguments.args + arguments.kwonlyargs]
    guard_names = {name for name in names if _GUARD_NAME_RE.fullmatch(name)}
    if not guard_names:
        return True

    bound = []
    for keyword in call.keywords:
        if keyword.arg in guard_names:
            bound.append(keyword.value)
    positional = arguments.posonlyargs + arguments.args
    for index, argument in enumerate(positional):
        if argument.arg in guard_names and index < len(call.args):
            bound.append(call.args[index])
    if not bound:
        return False

    def names_a_guard(node) -> bool:
        for sub in ast.walk(node):
            name = (sub.id if isinstance(sub, ast.Name)
                    else sub.value if isinstance(sub, ast.Constant)
                    and isinstance(sub.value, str) else None)
            if name and _GUARD_NAME_RE.fullmatch(name):
                return True
        return False

    return any(names_a_guard(value) for value in bound)


@functools.lru_cache(maxsize=1)
def _branches_reading_expected() -> frozenset[str]:
    """Dispatcher branches that reach a real `expected` read, by AST.

    Independent of `GUARD_CONTRACTS` on purpose: this is the code half of the
    comparison, so the dict cannot certify itself. Follows `ast.Name` callees to
    depth 2 and matches Phase A's corrected pattern `expected(_[ab])?`. The
    decoys are excluded by `fullmatch` rather than by a narrowed pattern, because
    a broader one produced a false positive twice -- `expected_type` names an
    asset type and `expected_prompt_template` a project-template content hash,
    and neither is a whole-identifier match.

    Bounded to the dispatcher, which is narrower than "the code half". The batch
    pre-pass in `_apply_scene_mutations_sync` validates `update_prompt_section`
    identity OUTSIDE this function; that operation also reads `expected` in its
    own branch, so nothing is missed today, but a guard added only to the
    pre-pass would leave a `_NOTHING` contract standing as a lie. Widen the walk,
    not the contract, if that ever happens.
    """
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    functions = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(node.name, node)
    dispatcher = functions.get("_apply_scene_mutation_operation")
    assert dispatcher is not None, "the dispatcher walk found no dispatcher"

    def reads_guard(node) -> bool:
        # Three shapes reach a guard: the parameter (`expected: dict | None`),
        # the local binding, and `op.get("expected")` -- where the name is a
        # string constant. `fullmatch` is what keeps prose out: a docstring or
        # comment saying "expected" is not one of these, and `expected_type` and
        # `expected_prompt_template` fail it as whole identifiers.
        for sub in ast.walk(node):
            name = (sub.id if isinstance(sub, ast.Name)
                    else sub.attr if isinstance(sub, ast.Attribute)
                    else sub.arg if isinstance(sub, ast.arg)
                    else sub.value if isinstance(sub, ast.Constant)
                    and isinstance(sub.value, str) else None)
            if name and _GUARD_NAME_RE.fullmatch(name):
                return True
        return False

    def honours(node, depth: int, seen: frozenset) -> bool:
        if reads_guard(node):
            return True
        if depth <= 0:
            return False
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)):
                continue
            callee = sub.func.id
            if callee in seen or callee not in functions:
                continue
            # A helper that TAKES a guard is only honouring one if this call
            # hands it a guard from the request. `_consolidate_media_items`
            # removes a lane it has just emptied and passes an explicit
            # server-state sentinel, so counting `_remove_media_lane`'s parameter
            # would report the whole `consolidate_items` branch as guarded while
            # it reads nothing at all from the operation.
            if not _hands_on_a_client_guard(functions[callee], sub):
                continue
            if honours(functions[callee], depth - 1, seen | {callee}):
                return True
        return False

    found = set()
    for statement in ast.walk(dispatcher):
        if not isinstance(statement, ast.If):
            continue
        test = statement.test
        if not (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                and test.left.id == "op_type"):
            continue
        for comparator in test.comparators:
            if not (isinstance(comparator, ast.Constant)
                    and isinstance(comparator.value, str)):
                continue
            body = ast.Module(body=statement.body, type_ignores=[])
            if honours(body, 2, frozenset()):
                found.add(comparator.value)
    return frozenset(found)


def _guarded_emissions():
    """Every emission carrying an `expected*`, keyed as the catalogues are."""
    sites = {}
    for item in _operation_literals():
        paths = _guard_key_paths(item["extent"], item["mask"])
        if not paths:
            continue
        key = f"{item['module']}:{item['scope']}:{item['op_type']}"
        sites.setdefault(key, {"op_type": item["op_type"], "line": item["line"],
                               "paths": set(), "fields": set()})
        sites[key]["paths"].update(paths)
        sites[key]["fields"].update(
            path.split(".", 1)[1]
            for path in _payload_fingerprint(item["extent"], item["mask"])
            if path.startswith("fields."))
    return sites


def test_every_dispatcher_operation_declares_a_guard_contract():
    """Coverage. A new operation must state what it does with an `expected`."""
    op_types = _dispatcher_op_types()
    missing = sorted(op_types - set(GUARD_CONTRACTS))
    assert not missing, (
        "these dispatcher operations declare no guard contract, so the tripwire "
        "below cannot tell a guard they honour from one they discard. Add an "
        f"entry naming the validating call, or its absence: {missing}")
    extra = sorted(set(GUARD_CONTRACTS) - op_types)
    assert not extra, f"contracts for operations the dispatcher no longer has: {extra}"
    for op_type, (kind, keys, evidence) in sorted(GUARD_CONTRACTS.items()):
        assert kind in {_FIXED, _WRITTEN_FIELDS, _WHOLE_RECORD, _PER_ITEM,
                        _NOTHING}, f"{op_type}: unknown contract kind {kind!r}"
        assert _cited_names(evidence), (
            f"{op_type}: the evidence names no code. Cite the handler the branch "
            "calls, in backticks")
        assert bool(keys) == (kind == _FIXED), (
            f"{op_type}: only a {_FIXED} contract carries a key set")


def test_the_guard_contracts_still_match_the_code():
    """The dict cannot certify itself; the AST is the other half.

    Fails in both directions. A branch that gains validation makes its
    `_NOTHING` entry a lie that would keep its emissions in the exemption
    catalogue forever; a branch that loses validation makes a positive entry a
    guard the server has quietly stopped honouring.
    """
    reading = _branches_reading_expected()
    declared_none = {op for op, (kind, _keys, _why) in GUARD_CONTRACTS.items()
                     if kind == _NOTHING}
    gained = sorted(declared_none & reading)
    assert not gained, (
        "these operations are recorded as reading no `expected`, but the "
        "dispatcher now reaches one. Re-read the branch and give it a contract "
        "naming the keys it COMPARES -- this walk matches the name, so a bare "
        "`op.get(\"expected\")` that is never compared is not a guard and a "
        "generous key set here would silence the tripwire for every emission on "
        "that operation. Then check whether those emissions can leave "
        f"GUARD_EXEMPT_SITES: {gained}")
    lost = sorted((set(GUARD_CONTRACTS) - declared_none) - reading)
    assert not lost, (
        "these operations declare a guard contract, but no `expected` read is "
        "reachable from their dispatch branch any more. A guard the server "
        f"stopped honouring is worse than one it never had: {lost}")


def test_no_emission_sends_a_guard_the_server_discards():
    """The tripwire this section exists for.

    `GUARD_EXEMPT_SITES` can be drained by adding an `expected` the server
    throws away -- the Phase 3 predicate matches a token on the client and
    cannot see the branch. This is what stops that, so read its failure as
    **add server-side validation**, not as add an exemption. Adding an
    exemption here re-opens the hole it closes.
    """
    findings = []
    for key, site in sorted(_guarded_emissions().items()):
        kind, honoured, _evidence = GUARD_CONTRACTS[site["op_type"]]
        for container, guard, field in sorted(site["paths"]):
            label = f"{container + '.' if container else ''}{guard}.{field}"
            if (key, label) in DISCARDED_GUARD_KEYS:
                continue
            # Opacity is checked LAST on purpose. For an operation that reads no
            # `expected` at all, the value's lexical shape is irrelevant -- every
            # key is discarded whatever it looks like -- so skipping opaque
            # values first would leave the catalogue drainable by writing
            # `expected: priorClip` instead of an inline literal. That is the
            # exact hole this section exists to close, wearing a different hat.
            if kind == _NOTHING:
                findings.append(
                    f"{key} (line {site['line']}) sends {label}, and the "
                    "dispatch branch reads no `expected` on any path")
            elif kind == _PER_ITEM and not container:
                findings.append(
                    f"{key} (line {site['line']}) sends {label} at the operation "
                    "level, but the branch reads `expected` only inside its "
                    "items collection")
            elif field == "<opaque>":
                continue  # OPAQUE_GUARD_SITES owns what a scan cannot read
            elif kind == _FIXED and field not in honoured:
                findings.append(
                    f"{key} (line {site['line']}) sends {label}, but the branch "
                    f"compares only {sorted(honoured)}")
            elif kind == _WRITTEN_FIELDS and site["fields"] and (
                    "<opaque>" not in site["fields"]) and field not in site["fields"]:
                findings.append(
                    f"{key} (line {site['line']}) sends {label}, but the branch "
                    f"compares only the written fields {sorted(site['fields'])}")
    assert not findings, (
        "an `expected` the server does not compare is not a guard -- it "
        "satisfies the Phase 3 catalogue and protects nothing. Add the "
        "comparison to the dispatch branch, or remove the key so the emission "
        "stops claiming a protection it does not have. Do NOT add an exemption "
        "unless the key is provably inert, and say why in DISCARDED_GUARD_KEYS: "
        + "; ".join(findings))


def _required_expected_key_sets():
    """`_require_expected(expected, {...}, "<op_type>")`, read out of routes.py.

    Only calls whose LABEL is a dispatcher operation type are collected, which
    is deliberate: the label is what ties a mandatory key set to the operation
    a client emits, and using the op type as the label is the convention this
    scan enforces by only being able to see branches that follow it. The older
    `_require_expected` sites label themselves by prose ("update_reference",
    "reference item mutation") and are out of scope for the same reason they
    are out of scope for the emission scanner: their payloads are built
    somewhere this file cannot follow.
    """
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    op_types = _dispatcher_op_types()
    required = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_require_expected" and len(node.args) >= 3):
            continue
        keys, label = node.args[1], node.args[2]
        if not (isinstance(label, ast.Constant) and label.value in op_types):
            continue
        if not (isinstance(keys, ast.Set) and all(
                isinstance(element, ast.Constant) and isinstance(element.value, str)
                for element in keys.elts)):
            continue
        required.setdefault(label.value, set()).update(
            element.value for element in keys.elts)
    return required


def test_an_emission_sends_every_key_its_branch_requires():
    """The direction the guard tripwire above cannot see, and the expensive one.

    `test_no_emission_sends_a_guard_the_server_discards` checks *sent is a
    subset of honoured* -- an extra key protects nothing and is caught. Nothing
    checked the reverse, and `_require_expected` is a key-SET check that answers
    a missing key with `400 missing_expected_identity`. So a refactor dropping
    one key from a client payload makes **every** such gesture fail at runtime
    while the whole suite stays green, because the backend tests build their own
    payloads and the Node tests build their own intents.

    That was harmless while the only `_require_expected` branches were Library
    routes whose payloads this file cannot read. Umbrella Phase C stage 2 L1
    made the media splits mandatory-guard operations on the timeline's most
    frequent gesture, which is what makes the gap worth a scan.

    Two directions, because they fail differently: an emission that sends SOME
    of the required keys, and an emission that carries no `expected` at all --
    the second is invisible to `_guarded_emissions` by construction.
    """
    required = _required_expected_key_sets()
    assert required, (
        "no dispatcher branch was read as requiring an `expected` key set. "
        "Either the AST walk has stopped matching `_require_expected`, or the "
        "labelling convention it depends on changed -- both make this tripwire "
        "silently vacuous")

    guarded = _guarded_emissions()
    emissions = {}
    for item in _operation_literals():
        if item["op_type"] in required:
            emissions.setdefault(
                f"{item['module']}:{item['scope']}:{item['op_type']}", item)

    findings = []
    for key, item in sorted(emissions.items()):
        needed = required[item["op_type"]]
        site = guarded.get(key)
        if not site:
            findings.append(
                f"{key} (line {item['line']}) sends no `expected` at all, but "
                f"its dispatch branch requires {sorted(needed)} -- every one of "
                "these gestures would 400 at runtime")
            continue
        sent = {field for container, guard, field in site["paths"]
                if not container and guard == "expected"}
        if "<opaque>" in sent:
            continue  # OPAQUE_GUARD_SITES owns what a scan cannot read
        missing = sorted(needed - sent)
        if missing:
            findings.append(
                f"{key} (line {site['line']}) omits {missing}, which its "
                f"dispatch branch requires -- this gesture would 400 at runtime")

    assert not findings, (
        "a mandatory `expected` key is missing from a client emission. This is "
        "a hard runtime failure, not a weakened guard: `_require_expected` "
        "refuses the whole batch with `400 missing_expected_identity` before "
        "comparing anything. Add the key to the emission, or -- if the guard "
        "genuinely should not require it -- narrow the set in the dispatch "
        "branch, the validator's `checks` and the transcribed key set together: "
        + "; ".join(findings))


def test_the_required_key_scan_reads_the_branches_it_claims():
    """Liveness. A scan that found nothing would pass the test above forever."""
    required = _required_expected_key_sets()
    assert required.get("split_clip") == {
        "clip_id", "timeline_start_frame", "track_index", "role"}, (
        f"split_clip's mandatory set reads as {sorted(required.get('split_clip', ()))}")
    assert required.get("split_audio_track") == {
        "track_id", "timeline_start_frame", "lane_index"}, (
        "split_audio_track's mandatory set reads as "
        f"{sorted(required.get('split_audio_track', ()))}")
    # The MANDATORY set and the COMPARED set are two reads of two different
    # pieces of code. They must agree, or one of them is decoration: a key
    # required but not compared forces the client to send something nothing
    # checks, and a key compared but not required is a guard the client can
    # silently stop supplying.
    assert required["split_clip"] == _CLIP_KEYS
    assert required["split_audio_track"] == _AUDIO_KEYS


def test_the_required_key_scan_would_catch_a_dropped_key():
    """Drive the scan against a payload missing two mandatory keys."""
    emission = _scan_text(
        'function g() { this._runSceneMutation([{ type: "split_clip", '
        'clip_id: id, frame, expected: { clip_id: id, timeline_start_frame: 0 } }]); }\n')
    assert emission, "the harness emission did not parse"
    sent = {field for container, guard, field
            in _guard_key_paths(emission[0]["extent"], emission[0]["mask"])
            if not container and guard == "expected"}
    missing = _required_expected_key_sets()["split_clip"] - sent
    assert missing == {"track_index", "role"}, (
        f"the scan reads the shortened payload as missing {sorted(missing)}; it "
        "must see exactly the two keys that were dropped")


def test_an_opaque_guard_is_catalogued_rather_than_assumed_honoured():
    """Opaque is a named class. Silence would be the false-confidence test."""
    opaque = {key for key, site in _guarded_emissions().items()
              if any(field == "<opaque>" for _c, _g, field in site["paths"])}
    unlisted = sorted(opaque - set(OPAQUE_GUARD_SITES))
    assert not unlisted, (
        "these emissions carry an `expected` the scan cannot read through, so "
        "the tripwire above skipped them. Record what makes each opaque and "
        "where its keys are built, or make the literal readable: "
        f"{unlisted}")
    gone = sorted(set(OPAQUE_GUARD_SITES) - opaque)
    assert not gone, (
        f"these opaque entries match no guarded emission any more: {gone}")


def test_discarded_guard_entries_are_not_stale():
    """An exemption must be able to die, or it is documentation of a gap."""
    live = set()
    for key, site in _guarded_emissions().items():
        for container, guard, field in site["paths"]:
            live.add((key, f"{container + '.' if container else ''}{guard}.{field}"))
    gone = sorted(entry for entry in DISCARDED_GUARD_KEYS if entry not in live)
    assert not gone, (
        "these discarded-guard entries no longer match any emission -- the key "
        f"was removed or the site changed -- so the entry should go with it: {gone}")

    # This is the cheapest way out of the tripwire above, so it is held to the
    # same standard as the catalogues it can override: a reason that cites code.
    # Left unpoliced it would be the least-defended dict in the file and the one
    # a maintainer reaches for first.
    unevidenced = sorted(entry for entry, note in DISCARDED_GUARD_KEYS.items()
                         if not _cited_names(note))
    assert not unevidenced, (
        "an exemption from the guard tripwire must cite the code proving the key "
        f"is inert: {unevidenced}")


def test_the_guard_extractor_reads_the_shapes_the_tree_uses():
    """Guards the guard, with exact findings on each live shape."""
    inline = _scan_text(
        'function gesture() { send({ type: "update_guide", frame_index: f,'
        ' expected: { guide_id: id, frame_index: f } }); }\n')
    assert _guard_key_paths(inline[0]["extent"], inline[0]["mask"]) == (
        ("", "expected", "frame_index"), ("", "expected", "guide_id"))

    # The shape a depth-1 walker cannot see at all: four live sites use it.
    spread = _scan_text(
        'function gesture() { send({ type: "update_lane_config", lane_index: i,'
        ' ...(laneId ? { expected: { lane_id: laneId } } : {}) }); }\n')
    assert _guard_key_paths(spread[0]["extent"], spread[0]["mask"]) == (
        ("", "expected", "lane_id"),)

    shorthand = _scan_text(
        'function gesture() { send({ type: "update_prompt_section", index: i,'
        ' expected, fields }); }\n')
    assert _guard_key_paths(shorthand[0]["extent"], shorthand[0]["mask"]) == (
        ("", "expected", "<opaque>"),)

    identifier = _scan_text(
        'function gesture() { send({ type: "delete_guide", frame_index: f,'
        ' expected: snapshot }); }\n')
    assert _guard_key_paths(identifier[0]["extent"], identifier[0]["mask"]) == (
        ("", "expected", "<opaque>"),)

    nested = _scan_text(
        'function gesture() { send({ type: "bulk_delete_items", items: ['
        '{ type: "prompt", id: 0, expected: { prompt_id: p } }] }); }\n')
    assert _guard_key_paths(nested[0]["extent"], nested[0]["mask"]) == (
        ("items[]", "expected", "prompt_id"),)

    # The two decoys. Certifying either is the specific error Phase A fixed and
    # this plan's first draft then repeated.
    for decoy in ("expected_type: \"video\"", "expected_prompt_template: hash"):
        literal = _scan_text(
            'function gesture() { send({ type: "update_clip", clip_id: id, '
            + decoy + " }); }\n")
        assert _guard_key_paths(literal[0]["extent"], literal[0]["mask"]) == (), decoy


def test_the_guard_tripwire_fires_on_a_guard_the_server_discards():
    """Prove the failure by hand, on the exact repair the ratchet invites.

    `update_clip` is the operation with the most exempted emissions, and the
    cheapest way to clear them from Phase 3 is to add an `expected` its branch
    never reads. That must fail here.
    """
    faked = _scan_text(
        'function gesture() { send({ type: "update_clip", clip_id: id,'
        ' fields: { timeline_start_frame: f },'
        ' expected: { timeline_start_frame: prior } }); }\n')
    assert _is_guarded(faked[0]["extent"], faked[0]["mask"]), (
        "the Phase 3 predicate reads this as guarded -- which is the hole")
    kind, _keys, _why = GUARD_CONTRACTS["update_clip"]
    assert kind == _NOTHING
    paths = _guard_key_paths(faked[0]["extent"], faked[0]["mask"])
    assert ("", "expected", "timeline_start_frame") in paths

    # And a guard on a field its branch really does compare must pass.
    honest = _scan_text(
        'function gesture() { send({ type: "update_scene_fields",'
        ' fields: { prompt: next }, expected: { prompt: prior } }); }\n')
    kind, honoured, _why = GUARD_CONTRACTS["update_scene_fields"]
    assert kind == _FIXED and "prompt" in honoured
    assert ("", "expected", "prompt") in _guard_key_paths(
        honest[0]["extent"], honest[0]["mask"])
    # ... while the same shape on a field it writes but never compares does not.
    assert "width" not in honoured


# ---------------------------------------------------------------------------
# Phase 5b -- a decision per SITE, not per operation
# ---------------------------------------------------------------------------
# Phase A wrote `GUARD_EXEMPT_REASONS` per operation and recorded that splitting
# it per site was the right thing, left to umbrella Phase B. This is that split.
# The reason an operation carries no guard is a property of the operation; whether
# that MATTERS is a property of the call site, and three operations here hold
# sites on both sides of the line.
#
# Three dispositions, and the middle one is the finding that made this worth
# doing: two emissions the scan calls unguarded are among the best-guarded writes
# in the tree, because their guard is built by a helper instead of written as a
# literal.
_FAIR = "fair"          # nothing an `expected` could name
_REASONED = "reasoned"  # guarded, or bounded, by something the scan cannot see
_GAP = "gap"            # a real gap; the note names who owns closing it

GUARD_SITE_DISPOSITIONS = {
    # -- `update_scene_fields`: the clearest per-field partial ---------------
    # The scene comes from the URL, so there is no row to identify; what an
    # `expected` protects here is the prior VALUE of a field. Four of the sixteen
    # fields get that (GUARD_CONTRACTS). None of these eight names one of them --
    # but two carry a stronger guard the scan structurally cannot read.
    "editor_prompt_panel.js:attachReference:update_scene_fields": (_REASONED,
        "`fields` is `promptEditFields` (prompt_edit_intent.js), which emits "
        "`prompt_edit: { attachments: { <id>: { expected, value } } }` -- a "
        "per-attachment exact-prior-value guard that `_merge_prompt_edit_fields` "
        "requires and then refuses on a content-hash mismatch. Unguarded is the "
        "scan's word, not the code's: the guard is built by a helper, so no "
        "`expected` token appears at the emission. Expiry: only if that helper "
        "stops emitting prompt_edit."),
    "editor_widget.js:_updateSceneGlobalContextWithinGesture:update_scene_fields": (
        _REASONED,
        "The same `promptEditFields` guard, per document and per attachment, plus "
        "`expected_prompt_template` -- a project-template content hash checked at "
        "the top of `_apply_scene_mutation_operation`, before any branch. "
        "Deliberately not matched by `_GUARD_RE`, because it is not a claim about "
        "a row."),
    "editor_prompt_panel.js:openProfileEditor:update_scene_fields": (_FAIR,
        "Writes `prompt_context_profile_id`, one whole-value id set. Last write "
        "wins is the correct semantic for a whole-value set: an `expected` would "
        "refuse the second of two profile switches with no wrong outcome to "
        "prevent."),
    "editor_prompt_panel.js:renderContextSettings:update_scene_fields": (_FAIR,
        "The same `prompt_context_profile_id` whole-value set as "
        "`openProfileEditor`, reached from the Context settings surface."),
    "editor_widget.js:_renameSceneWithinGesture:update_scene_fields": (_FAIR,
        "Whole-value `name` set. §1b of the plan established the same reading for "
        "rename, duration and resolution: idempotent whole-value sets carrying no "
        "`expected`, so last-write-wins is correct and the coalescing collapse is "
        "not a defect."),
    "editor_widget.js:_updateSceneDurationWithinGesture:update_scene_fields": (_FAIR,
        "Whole-value `duration_frames` set. Note the tail effect is NOT unguarded "
        "collateral: `_apply_scene_fields` calls `_clamp_reference_items_to_scene`, "
        "which is derived from the new duration rather than from client state."),
    "editor_widget.js:_updateSceneResolutionWithinGesture:update_scene_fields": (_FAIR,
        "Whole-value `width`/`height` set."),
    "editor_widget.js:_updateSceneFpsWithinGesture:update_scene_fields": (_FAIR,
        "Whole-value `fps` set, and the one field here with a real server-side "
        "precondition of its own: `_require_scene_queue_idle` refuses the change "
        "while that scene has pending or running jobs, and `retime_scene_geometry` "
        "derives every endpoint from the stored value rather than from anything "
        "the client computed."),

    # -- `update_clip` / `update_audio_track`: 15 sites, one gap ------------
    # Sized here, implemented by a successor plan. The sizing fact this adds to
    # what split-optimistic-local-apply.md already recorded: the gap is
    # SERVER-SIDE FIRST. `_apply_update_clip` and `_apply_update_audio_track`
    # read no `expected` on either the plain or the apply_linked path, so adding
    # one to any of these 15 emissions today would change nothing at all.
    **{site: (_GAP,
              "`update_clip`/`update_audio_track` carry timeline geometry against a "
              "durable id with no prior-value check anywhere on the path. "
              "Server-side first: the branch must learn to compare before any "
              "emission is worth changing. Owned by a successor plan; the "
              "enabling condition for the tracked entry about items sitting past "
              "the end of their own media.")
       for site in (
           "editor_widget.js:_commitItemMove:update_audio_track",
           "editor_widget.js:_commitItemMove:update_clip",
           "editor_widget.js:_commitTrim:update_audio_track",
           "editor_widget.js:_commitTrim:update_clip",
           "editor_widget.js:_convertClipRoleWithinGesture:update_clip",
           "editor_widget.js:_moveItemToFrameWithinGesture:update_audio_track",
           "editor_widget.js:_moveItemToFrameWithinGesture:update_clip",
           "editor_widget.js:_moveItemToNewLaneWithinGesture:update_audio_track",
           "editor_widget.js:_moveItemToNewLaneWithinGesture:update_clip",
           "editor_widget.js:_muteOperationForItem:update_audio_track",
           "editor_widget.js:_muteOperationForItem:update_clip",
           "editor_widget.js:_toggleSelectedMuteWithinGesture:update_audio_track",
           "editor_widget.js:_toggleSelectedMuteWithinGesture:update_clip",
           "editor_widget.js:_updateItemPropertyWithinGesture:update_audio_track",
           "editor_widget.js:_updateItemPropertyWithinGesture:update_clip",
       )},

    # -- creates that really are additive ------------------------------------
    "editor_widget.js:_handleAssetDropWithinGesture:create_clip": (_FAIR,
        "Two fingerprints, both additive: no prior row exists for an `expected` "
        "to describe. Its `track_index` and `audio_lane_index` are the rebase "
        "question, and `case \"create_clip\"` retargets both."),
    "editor_widget.js:_handleAssetDropWithinGesture:create_audio_track": (_FAIR,
        "Additive; no prior row exists. Its `lane_index` is the rebase question "
        "rather than this one, and `case \"create_audio_track\"` retargets it."),
    "editor_widget.js:_saveNewPromptSectionWithinGesture:create_prompt_section": (_FAIR,
        "Additive, and a stale range does not apply silently: "
        "`_require_no_prompt_overlap` refuses a range history has since occupied. "
        "The same argument REBASE_EXEMPT records for this operation."),
    "editor_widget.js:_applyPromptSetupWithinGesture:create_prompt_semantic_unit": (_FAIR,
        "Mints project-level prompt identity through "
        "`_apply_create_prompt_semantic_unit`. No scene row exists yet or is "
        "named."),
    "editor_widget.js:_runRedoWithinGesture:create_prompt_semantic_unit": (_FAIR,
        "As `_applyPromptSetupWithinGesture`; Redo re-mints the same identity, and "
        "`delete_prompt_semantic_unit_if_unreferenced` is the guarded half of "
        "that pair."),
    "prompt_context_chips.js:configurePromptAttachment:create_prompt_semantic_unit": (
        _FAIR, "The same `create_prompt_semantic_unit` mint, from the shared "
        "`prompt_context_chips.js` editor."),
    "editor_widget.js:_applyPromptSetupWithinGesture:import_prompt_context_dependencies": (
        _FAIR,
        "Project-level profile and semantic-unit closures; no scene row is "
        "addressed. Carried, not closed: `_apply_prompt_context_dependencies` "
        "`extend`s the project lists, so two COALESCED imports would drop the "
        "older one's items. Its only emitter is `coalesce: false`, which is what "
        "keeps this fair rather than a gap -- Phase 4's residual note owns the "
        "day that changes."),

    # -- creates that are not purely additive --------------------------------

    # -- collections the client assembled ------------------------------------
    "editor_widget.js:_createLinkGroupFromSelectionWithinGesture:create_link_group": (
        _REASONED,
        "Its whole payload is references to pre-existing rows, and the client "
        "DOES build per-item `expected` -- `_item_ref_from_selection` (:1327) "
        "discards it. The positional half of that exposure is now closed on the "
        "client side: umbrella Phase B gave this operation a rebase case, so a "
        "queued history action retargets the refs instead of letting them drift. "
        "What remains is a concurrent OTHER writer, which only the server-side "
        "guard would catch. Reasoned rather than fair, and reasoned rather than "
        "gap, because the reachable half is fixed."),
    "editor_widget.js:_unlinkSelectedItemsWithinGesture:unlink_items": (_REASONED,
        "The same `_mutationItemFromSelection` refs and the same rebase case; "
        "unlinking a wrong row is also recoverable in a way that deleting one "
        "is not."),
    "editor_widget.js:_deleteItemsInLaneWithinGesture:bulk_delete_items": (_FAIR,
        "Carries durable clip/audio ids and a boolean. There is no prior row "
        "state an `expected` would describe, and clip/audio are exactly the two "
        "item types `_apply_bulk_delete_items` resolves by durable id."),
    "editor_widget.js:_deleteSelectedItemsWithinGesture:bulk_delete_items": (_REASONED,
        "`items` is an identifier the scan cannot see through, and the array DOES "
        "carry per-item `expected` -- built by `_mutationItemFromSelection` -- "
        "that the server validates for guide, prompt and reference. The residual "
        "that made this a gap is closed: `_validate_selection_item_identity` now "
        "runs ahead of `_item_ref_from_selection` on the apply_linked path, so a "
        "guarded delete no longer becomes an unguarded one by being linked. Clip "
        "and audio members carry no snapshot and need none -- they are addressed "
        "by durable id."),
    "editor_widget.js:_consolidateSelectedItemsToLaneWithinGesture:consolidate_items": (
        _FAIR,
        "Carries `target_lane` and durable `item_ids`. The lane index is rebased "
        "through `rebaseLaneIndex`, and `_consolidate_media_items` range-checks it "
        "and refuses an id it cannot resolve. The item ids are durable, so "
        "nothing here is addressed by position without a check."),
}


def test_every_unguarded_emission_has_a_site_disposition():
    """Per site, not per operation. 36 emissions, 35 keys, one decision each.

    The count falls as guards land -- it was 47/46 before umbrella Phase B, and
    Phase C stage 2 L1 took the two media splits out. Measured from
    `_unguarded_payload_sites()`, never typed.
    """
    sites = _unguarded_payload_sites()
    undecided = sorted(set(sites) - set(GUARD_SITE_DISPOSITIONS))
    assert not undecided, (
        "these emissions carry an operation-level reason but no decision about "
        "this call site. Say whether the missing guard is fair here, reasoned "
        f"here, or a gap here, and trace it: {undecided}")
    gone = sorted(set(GUARD_SITE_DISPOSITIONS) - set(sites))
    assert not gone, (
        "these dispositions match no unguarded emission any more -- the site was "
        f"guarded, renamed or removed -- so the decision should go with it: {gone}")
    for key, (disposition, note) in sorted(GUARD_SITE_DISPOSITIONS.items()):
        assert disposition in {_FAIR, _REASONED, _GAP}, f"{key}: {disposition!r}"
        # A disposition is a claim about code, so it has to cite some -- a source
        # file, a plan, or an identifier in backticks. A character count would be
        # an arbitrary proxy whose cheapest repair is padding.
        assert re.search(r"routes\.py|\w+\.(?:js|md)|`[A-Za-z_][\w.]*`",
                         note), (
            f"{key}: the disposition cites nothing checkable. Name the handler, "
            "the helper, or the plan that owns it")


def test_the_site_dispositions_agree_with_the_operation_reasons():
    """The two catalogues must cover the same surface, keyed differently."""
    per_site_ops = {key.rsplit(":", 1)[-1] for key in GUARD_SITE_DISPOSITIONS}
    assert per_site_ops == set(GUARD_EXEMPT_REASONS), (
        "the per-site and per-operation catalogues disagree about which "
        "operations are exempted: "
        f"{sorted(per_site_ops ^ set(GUARD_EXEMPT_REASONS))}")


def test_no_catalogue_still_calls_a_decided_operation_unreviewed():
    """Prose drifts silently; this is the one claim worth policing mechanically.

    Four `GUARD_EXEMPT_REASONS` entries went on saying "unreviewed" after the
    dict that held the unreviewed entries had been emptied, so two catalogues in
    this file said opposite things about `create_guide` and nothing failed. The
    word is a status claim about another dict, which makes it checkable.
    """
    stale = sorted(
        op_type for op_type, reason in GUARD_EXEMPT_REASONS.items()
        if re.search(r"\bunreviewed\b", reason, re.IGNORECASE)
        and op_type not in REBASE_UNREVIEWED)
    assert not stale, (
        "these reasons call an operation unreviewed, but REBASE_UNREVIEWED no "
        "longer holds it -- so the reason is describing a state that ended. "
        f"Say what the review concluded: {stale}")

    for name, entries in (("REBASE_EXEMPT", REBASE_EXEMPT),
                          ("DELIBERATE_NO_PROJECTION", DELIBERATE_NO_PROJECTION)):
        contradictory = sorted(
            op_type for op_type, reason in entries.items()
            if re.search(r"\bunreviewed\b", reason, re.IGNORECASE))
        assert not contradictory, (
            f"{name} explains these operations while calling them unreviewed: "
            f"{contradictory}")


def test_the_dispositions_hold_the_findings_the_trace_established():
    """Not a count -- the specific conclusions, so reclassifying one fails.

    An earlier version asserted only `>= 2` gaps and `>= 2` fair, which 25 of the
    27 gaps could be reclassified without tripping. An exact total is no better:
    its cheapest repair is to bump the number, the failure Phase A's audit found
    repeatedly. What is worth pinning is what the trace CONCLUDED, per operation,
    because each of these is a claim someone would have to re-refute to change.
    """
    by_op = {}
    for key, (disposition, _note) in GUARD_SITE_DISPOSITIONS.items():
        by_op.setdefault(key.rsplit(":", 1)[-1], set()).add(disposition)

    # Every emission of these is a gap: the branch compares nothing and the
    # payload names a row or a lane the client read from a view that can be stale.
    # `split_clip` and `split_audio_track` were here until umbrella Phase C
    # stage 2 L1 gave both a dispatch-branch guard; they now carry a _FIXED
    # contract instead, which is what drained their disposition entries.
    for op_type in ("update_clip", "update_audio_track"):
        assert by_op.get(op_type) == {_GAP}, (
            f"{op_type} was traced as a gap at every site; it now reads as "
            f"{sorted(by_op.get(op_type, ()))}. Re-refute the trace before "
            "changing it")

    # And these are fair at every site: additive, or a whole-value set where
    # last-write-wins is the correct semantic.
    for op_type in ("create_clip", "create_audio_track", "create_prompt_section",
                    "create_prompt_semantic_unit", "consolidate_items",
                    "import_prompt_context_dependencies"):
        assert by_op.get(op_type) == {_FAIR}, (
            f"{op_type} was traced as fair at every site; it now reads as "
            f"{sorted(by_op.get(op_type, ()))}")

    # `bulk_delete_items` is the one operation whose two sites still disagree,
    # which is the whole reason this catalogue is per site rather than per
    # operation. One carries durable ids and needs nothing; the other carries a
    # per-item snapshot the linked path used to discard.
    assert by_op.get("bulk_delete_items") == {_FAIR, _REASONED}, (
        "bulk_delete_items' two sites used to disagree. If they now agree, "
        "either the code changed or the distinction was lost")

    assert _REASONED in {d for values in by_op.values() for d in values}, (
        "no site reads as reasoned. The two `promptEditFields` sites are why the "
        "class exists: their guard is built by a helper, so a lexical scan calls "
        "the best-guarded writes in the tree unguarded")


def test_the_guard_evidence_cites_code_that_exists():
    """A traced reason has to be traceable.

    The first pass of `GUARD_CONTRACTS` cited `routes.py:<line>` and sixteen of
    those lines landed on the PREVIOUS branch's tail -- generated from an index
    instead of read. A line number cannot be checked and rots under any edit
    above it; a backticked identifier can be checked, so that is what the
    evidence carries and this is the check.
    """
    known = _routes_identifiers()
    anywhere = known | _client_identifiers()
    unknown = []
    for op_type, (_kind, _keys, evidence) in sorted(GUARD_CONTRACTS.items()):
        for name in _cited_names(evidence):
            # A leading underscore marks an identifier rather than prose. Field
            # names and payload keys also ride in backticks and are not defined
            # anywhere, which is why the rule keys on the underscore.
            if name.startswith("_") and name not in anywhere:
                unknown.append(f"{op_type}: `{name}`")
    assert not unknown, (
        "these evidence entries cite an identifier neither routes.py nor an "
        "emitting module defines, so the trace points at nothing. Re-read the "
        f"branch: {unknown}")

    # And the citation must not be vacuous: every entry names at least one
    # routes.py function, which is what "traced to the handler" means.
    untraced = sorted(
        op_type for op_type, (_k, _keys, evidence) in GUARD_CONTRACTS.items()
        if not any(name in known and name.startswith("_")
                   for name in _cited_names(evidence)))
    assert not untraced, (
        "these entries cite no routes.py function at all, so nothing anchors the "
        f"claim to code: {untraced}")


def test_the_transcribed_key_sets_match_what_routes_compares():
    """Three key sets are copied out of routes.py; copies drift.

    The unsafe direction is silent: if a key is REMOVED server-side the
    transcription stays too wide, and an emission sending the now-discarded key
    passes the tripwire. So the sets are re-read from routes.py here rather than
    trusted, in the spirit of Phase A's reuse rule -- consume, never re-list.
    """
    assert _GUIDE_KEYS == _validator_key_set("_validate_guide_identity"), (
        "_GUIDE_KEYS no longer matches the keys `_validate_guide_identity` "
        f"compares: {sorted(_GUIDE_KEYS ^ _validator_key_set('_validate_guide_identity'))}")
    assert _PROMPT_KEYS == _validator_key_set("_validate_prompt_identity"), (
        "_PROMPT_KEYS no longer matches the keys `_validate_prompt_identity` "
        f"compares: {sorted(_PROMPT_KEYS ^ _validator_key_set('_validate_prompt_identity'))}")
    assert _CLIP_KEYS == _validator_key_set("_validate_clip_identity"), (
        "_CLIP_KEYS no longer matches the keys `_validate_clip_identity` "
        f"compares: {sorted(_CLIP_KEYS ^ _validator_key_set('_validate_clip_identity'))}")
    assert _AUDIO_KEYS == _validator_key_set("_validate_audio_identity"), (
        "_AUDIO_KEYS no longer matches the keys `_validate_audio_identity` "
        f"compares: {sorted(_AUDIO_KEYS ^ _validator_key_set('_validate_audio_identity'))}")
    honoured = GUARD_CONTRACTS["update_scene_fields"][1]
    assert honoured == _direct_global_prompt_fields(), (
        "update_scene_fields' honoured set no longer matches "
        "_DIRECT_GLOBAL_PROMPT_FIELDS: "
        f"{sorted(honoured ^ _direct_global_prompt_fields())}")


def test_the_two_depth_one_key_readers_agree():
    """`_object_literal_keys` and `_payload_fingerprint` must read alike.

    They are separate because they answer different questions -- one walks an
    operation literal to depth 2 keyed on PAYLOAD_KEYS, the other reads one
    nested object -- but they share a heuristic, and the copy has already drifted
    once: a non-leading spread was invisible to it while the original degraded
    correctly. This pins them together on the shapes both can see.
    """
    def fingerprint_keys(body):
        literal = _scan_text(
            'function g() { send({ type: "update_clip", fields: %s }); }\n' % body)
        return tuple(sorted(
            {path.split(".", 1)[1]
             for path in _payload_fingerprint(literal[0]["extent"], literal[0]["mask"])
             if path.startswith("fields.")}))

    for body, expected in (
            ("{ a: 1, b: 2 }", ("a", "b")),
            ("{ a, b }", ("a", "b")),
            ("{ a: { deep: 1 }, b: 2 }", ("a", "b")),
            ("{ a: [1, 2], b: 2 }", ("a", "b")),
            ("{ a: 1, ...rest }", ("<opaque>", "a")),
    ):
        assert _object_literal_keys(body) == expected, (
            f"{body}: _object_literal_keys gave {_object_literal_keys(body)}")
        assert fingerprint_keys(body) == expected, (
            f"{body}: _payload_fingerprint gave {fingerprint_keys(body)}")

    # One deliberate divergence, pinned rather than papered over. On a LEADING
    # spread `_payload_fingerprint` stops descending and records `<opaque>`
    # alone; its output is pinned in GUARD_EXEMPT_SITES, so widening it would
    # rewrite fingerprints across the catalogue for no gain. The guard reader has
    # no pinned output and reports both, which is strictly more information.
    assert fingerprint_keys("{ ...rest, a: 1 }") == ("<opaque>",)
    assert _object_literal_keys("{ ...rest, a: 1 }") == ("<opaque>", "a")


def test_a_guard_the_branch_ignores_cannot_hide_behind_an_identifier():
    """The drain the opacity skip used to allow, pinned shut.

    `expected: priorClip` reads as guarded to the Phase 3 predicate -- so the
    exemption entry must go -- while the server discards it whole. When opacity
    was checked before the contract, that swap was free and silent.
    """
    opaque = _scan_text(
        'function gesture() { send({ type: "update_clip", clip_id: id,'
        ' fields: { timeline_start_frame: f }, expected: priorClip }); }\n')
    assert _is_guarded(opaque[0]["extent"], opaque[0]["mask"])
    paths = _guard_key_paths(opaque[0]["extent"], opaque[0]["mask"])
    assert paths == (("", "expected", "<opaque>"),)
    assert GUARD_CONTRACTS["update_clip"][0] == _NOTHING

    # A spread that hides keys behind a readable one must not be certified by it.
    mixed = _scan_text(
        'function gesture() { send({ type: "update_scene_fields",'
        ' fields: { prompt: next },'
        ' expected: { prompt: prior, ...geometrySnapshot } }); }\n')
    fields = _guard_key_paths(mixed[0]["extent"], mixed[0]["mask"])
    assert ("", "expected", "prompt") in fields
    assert ("", "expected", "<opaque>") in fields, (
        "a trailing spread inside the guard hid its keys behind the readable one")


# ---------------------------------------------------------------------------
# Phase 5c -- a refusal the user cannot read is not a refusal
# ---------------------------------------------------------------------------
# `_queueProjectMutation` sets `error.code` only for `project_version_conflict`
# (api_client.js), so an `identity_mismatch` falls to the generic
# "... failed -- timeline restored." and the server's message is discarded. The
# whole point of a guard is that it says WHAT moved, and the plan's L3b requires
# it: "refuse with a message naming what moved". Every emission of a guarded
# operation must therefore pass `failureMessage`, because the default drops the
# only actionable thing the user gets.
#
# Scoped to operations whose guard can refuse a gesture the user just performed.
# An operation with no guard has nothing specific to say.
GUARDED_OPERATIONS_NEEDING_A_MESSAGE = frozenset({
    "remove_lane", "create_guide", "move_guide",
    "create_reference_item", "replace_prompt_sections", "bulk_delete_items",
    "create_link_group", "unlink_items",
    # Added by umbrella Phase C stage 2 L1. A stale cut used to be the quietest
    # failure in the editor -- 200, `split_count: 0`, no change and no message.
    # Now that the dispatch refuses it, the refusal has to reach the user, or
    # the landing has replaced a silent no-op with a silent rollback.
    "split_clip", "split_audio_track",
})


# Two named reasons a scope is judged by something other than its own body.
# Both are properties of the call site, not of the operation, which is why they
# are listed rather than computed -- a computed version would have to guess at
# what a shared runner is.
MESSAGE_OWNED_ELSEWHERE = {
    "editor_reference_panel.js:createItem":
        "Delegates its enqueue to `runItemOperation`, which owns the catch and "
        "already notifies with `error?.message`. The scope itself never calls "
        "`_runSceneMutation`, so its own body cannot show a message.",
    "editor_widget.js:_planItemSplit":
        "A PURE planner. It builds the operation and never enqueues, because "
        "planning every target before `_pushUndo` is what makes a wholly "
        "refused split gesture history-neutral by construction. The single "
        "enqueue lives in `_splitItemsAtFrameWithinGesture` and passes the "
        "`failureMessage` this test looks for; "
        "`test_the_split_runner_surfaces_the_server_message` pins that, so "
        "the exemption cannot outlive the thing it assumes.",
    "editor_widget.js:_deleteItemsInLaneWithinGesture":
        "Its `items` are clip and audio ids with no snapshot -- "
        "`_mutationItemFromSelection` builds one only for guide, prompt and "
        "reference -- so no identity guard can refuse this emission and there is "
        "no specific message for it to surface.",
}


def test_a_guarded_emission_surfaces_the_server_message():
    """An emission whose server branch can 409 must not use the generic toast."""
    silent = []
    for item in sorted(_operation_literals(), key=lambda i: (i["module"], i["scope"])):
        if item["op_type"] not in GUARDED_OPERATIONS_NEEDING_A_MESSAGE:
            continue
        if f"{item['module']}:{item['scope']}" in MESSAGE_OWNED_ELSEWHERE:
            continue
        scope_body = _code_only(_scope_body(item))
        # Two mechanisms reach the user, and both are live in the tree:
        # `failureMessage` on the enqueue, and a catch that notifies with the
        # error's own message. `_commitItemMove` uses the second.
        surfaced = ("failureMessage" in scope_body
                    or re.search(r"notify\w*\(\s*\w+\?\.message", scope_body))
        if not surfaced:
            silent.append(f"{item['module']}:{item['scope']}:{item['op_type']} "
                          f"(line {item['line']})")
    assert not silent, (
        "these emissions can be refused by a guard that names exactly what "
        "moved, and pass no `failureMessage` -- so `_queueProjectMutation` "
        "replaces the server's message with the generic \"... failed -- timeline "
        "restored.\" and the user is told nothing. `error.message` carries it; "
        "`_consolidateSelectedItemsToLaneWithinGesture` is the worked example. "
        f"{silent}")

    # The exemptions must be able to die with the sites they excuse.
    scopes = {f"{item['module']}:{item['scope']}" for item in _operation_literals()}
    gone = sorted(set(MESSAGE_OWNED_ELSEWHERE) - scopes)
    assert not gone, f"these message exemptions name no emitting scope: {gone}"


def test_the_split_runner_surfaces_the_server_message():
    """The pin under `_planItemSplit`'s MESSAGE_OWNED_ELSEWHERE exemption.

    That exemption is a claim about a DIFFERENT method than the one being
    excused, so the staleness check above -- which only asks whether the
    exempted scope still emits -- cannot see it go wrong. A refactor that
    dropped `failureMessage` from the runner would leave the planner exempt
    and the guard refusals silent again, which is the exact regression stage 2
    L1 landed the guards to prevent.
    """
    source = (JS_DIR / "editor_widget.js").read_text(encoding="utf-8")
    body = next(source[start:end] for name, start, end in _scopes(source)
                if name == "_splitItemsAtFrameWithinGesture")
    assert "this._runSceneMutation(" in body, (
        "the split runner no longer enqueues, so `_planItemSplit`'s message "
        "exemption names an owner that does nothing")
    assert "failureMessage" in body, (
        "`_splitItemsAtFrameWithinGesture` passes no `failureMessage`, so the "
        "409 naming the field that moved is replaced by the generic toast -- "
        "and `_planItemSplit` is exempt from noticing")


def _scope_body(item) -> str:
    """The source of the named scope that owns one emission."""
    source = (JS_DIR / item["module"]).read_text(encoding="utf-8")
    for name, start, end in _scopes(source):
        if name == item["scope"] and start <= item["offset"] <= end:
            return source[start:end]
    raise AssertionError(f"no scope body found for {item['scope']}")


# ---------------------------------------------------------------------------
# Phase 5 -- an enqueue that CANNOT coalesce says why
# ---------------------------------------------------------------------------
# The tripwire above is one-way: it asks whether an opted-IN gesture is safe.
# Nothing asks the opposite question, and the opposite question is where the
# measured cost is -- six lane-header clicks at 24.2 s, one burst at 460 s,
# because every gesture pays the route floor on its own.
#
# There are TWO spellings of "this will not coalesce" and only one of them is
# visible to a naive scan:
#
#   1. `coalesce: false`, an explicit decision. 17 live sites -- it was 20
#      before umbrella Phase C stage 1 retired three of them.
#   2. A key that can never repeat, because an interpolation in it changes on
#      every call. 29 live sites -- the MORE common spelling, and the flag is
#      redundant wherever it appears beside one.
#
# A uniquified key defeats coalescing exactly as well as the flag, so a review
# that only looked at flags would report two thirds of the surface as reviewed
# while never having read its keys. Both are findings here, classified
# differently because the reason each owes is different: a flag owes "why we
# refuse a merge we could have", a key owes "why two of these must never be one
# write".
#
# Keyed `module:scope:key-shape`, never a line number: a line-keyed entry rots
# on an unrelated edit above it, and the staleness test would then report the
# site as fixed when nothing about it had changed. The key shape is the right
# discriminator for a tripwire about keys -- it distinguishes the two enqueues
# inside one scope, and an entry dies the moment the key it describes changes.

# Pinned rather than bounded: the split between the two spellings is the
# finding that justified two tripwires instead of one.
EXPECTED_STABLE_KEY_OPT_OUTS = 17
EXPECTED_UNIQUIFIED_KEY_OPT_OUTS = 29
EXPECTED_CALLER_SUPPLIED_OPT_OUTS = 1

STABLE = "stable"
UNIQUIFYING = "uniquifying"

DECLINED = "declined"
UNREACHABLE = "unreachable"
CALLER_SUPPLIED = "caller_supplied"


def _skip_quoted(source: str, index: int) -> int:
    """Index just past the plain string opening at `index`."""
    quote = source[index]
    index += 1
    while index < len(source):
        if source[index] == "\\":
            index += 2
            continue
        if source[index] == quote:
            return index + 1
        if source[index] == "\n":
            return index
        index += 1
    return index


def _template_end(source: str, start: int) -> int:
    """Index of the backtick closing the template literal opening at `start`.

    Written rather than reusing `_code_mask`, which treats a template as one
    flat string and so mistakes a NESTED template's opening backtick for the
    outer one's close. One live key nests -- `_deleteSelectedLanesAndItemsWithinGesture`
    builds its key from `operations.map(...)` -- so the flat reading cuts that
    key in half and loses the interpolation that names the lanes.
    """
    index = start + 1
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == "`":
            return index
        if char == "$" and source[index + 1:index + 2] == "{":
            close = _interpolation_end(source, index + 1)
            if close < 0:
                return -1
            index = close + 1
            continue
        index += 1
    return -1


def _interpolation_end(source: str, brace: int) -> int:
    """Index of the `}` closing the `${` whose brace is at `brace`.

    Comments are skipped, because a `}` inside one closes the interpolation
    early and the expression recorded is then a fragment -- which fails
    `test_every_key_interpolation_is_classified` loudly, but names the wrong
    thing. Regex literals are deliberately NOT tracked, for the reason
    `_code_mask` gives: telling `/` division from a regex needs token context,
    and no live key contains one. A key that grows one fails the same
    classification test rather than passing quietly.
    """
    depth = 0
    index = brace
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == "/" and source[index + 1:index + 2] == "/":
            stop = source.find("\n", index)
            index = len(source) if stop < 0 else stop
            continue
        if char == "/" and source[index + 1:index + 2] == "*":
            stop = source.find("*/", index + 2)
            index = len(source) if stop < 0 else stop + 2
            continue
        if char == "`":
            close = _template_end(source, index)
            if close < 0:
                return -1
            index = close + 1
            continue
        if char in "'\"":
            index = _skip_quoted(source, index)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _template_parts(source: str, start: int) -> tuple[str, tuple[str, ...]]:
    """`(shape with every interpolation as ${}, the interpolated expressions)`."""
    end = _template_end(source, start)
    if end < 0:
        return "<unterminated>", ()
    shape, expressions = [], []
    index = start + 1
    while index < end:
        char = source[index]
        if char == "\\":
            shape.append(source[index:index + 2])
            index += 2
            continue
        if char == "$" and source[index + 1:index + 2] == "{":
            close = _interpolation_end(source, index + 1)
            expressions.append(
                re.sub(r"\s+", " ", source[index + 2:close]).strip())
            shape.append("${}")
            index = close + 1
            continue
        shape.append(char)
        index += 1
    return "".join(shape), tuple(expressions)


_KEY_RE = re.compile(r"\bkey\s*(?P<form>:|,|\})")
_KEY_DEFAULT = "<_runSceneMutation default>"
_KEY_UNRESOLVED = "<unresolved>"


def _binding_templates(source: str, span, name: str) -> tuple[str, tuple[str, ...]]:
    """Every template literal in `const <name> = ...`, up to its statement end.

    A key can be a ternary over two templates -- `_updateItemPropertyWithinGesture`
    picks a different shape for a one-field write than for a multi-field one --
    so both branches are read and both contribute interpolations. Taking only
    the first would classify the site on half its evidence.
    """
    if not span:
        return _KEY_UNRESOLVED, ()
    body = source[span[1]:span[2]]
    match = re.search(r"\b(?:const|let|var)\s+" + re.escape(name) + r"\s*=", body)
    if not match:
        return _KEY_UNRESOLVED, ()
    index = span[1] + match.end()
    shapes, expressions = [], []
    while index < span[2]:
        char = source[index]
        if char == ";":
            break
        if char == "`":
            shape, found = _template_parts(source, index)
            shapes.append(shape)
            expressions.extend(found)
            index = _template_end(source, index) + 1
            continue
        if char in "'\"":
            index = _skip_quoted(source, index)
            continue
        index += 1
    if not shapes:
        return _KEY_UNRESOLVED, ()
    return " | ".join(shapes), tuple(expressions)


def _enqueue_key(source: str, mask: bytearray, span, paren: int,
                 end: int) -> tuple[str, tuple[str, ...]]:
    """The key this enqueue takes, as `(shape, interpolated expressions)`.

    Walks the call rather than searching it, because the enqueue's FIRST
    argument is a list of operations and an operation may legitimately carry a
    field called `key`. A plain search takes the first match and would file the
    site under a key that does not exist -- silently, since the shape it records
    still looks like a key. Only a `key` directly inside the options object
    counts: array depth 0, brace depth 1, paren depth 1.

    An absent `key:` is NOT unresolved: `_runSceneMutation` supplies
    `scene:${targetSceneId}:mutation`, which repeats. Reading it as unresolved
    would report the default -- the most coalescible key in the file -- as a
    site nobody can classify. A SPREAD in the options object is different: the
    key may be in there and the scan cannot see it, so that is unresolved and
    fails closed.
    """
    parens = braces = arrays = 0
    spread_in_options = False
    index = paren
    while index <= end:
        if not mask[index]:
            index += 1
            continue
        char = source[index]
        if char == "(":
            parens += 1
        elif char == ")":
            parens -= 1
        elif char == "[":
            arrays += 1
        elif char == "]":
            arrays -= 1
        elif char == "{":
            braces += 1
        elif char == "}":
            braces -= 1
        at_options = parens == 1 and braces == 1 and arrays == 0
        if at_options and source.startswith("...", index):
            spread_in_options = True
        if at_options and source.startswith("key", index):
            match = _KEY_RE.match(source, index)
            if match:
                if match.group("form") != ":":
                    return _binding_templates(source, span, "key")
                value = match.end()
                while value < end and source[value] in " \n\t\r":
                    value += 1
                if source[value] == "`":
                    return _template_parts(source, value)
                if source[value] in "'\"":
                    stop = _skip_quoted(source, value)
                    return source[value + 1:stop - 1], ()
                identifier = re.match(r"[A-Za-z_$][\w$]*", source[value:])
                if not identifier:
                    return _KEY_UNRESOLVED, ()
                return _binding_templates(source, span, identifier.group(0))
        index += 1
    if spread_in_options:
        return _KEY_UNRESOLVED, ()
    return _KEY_DEFAULT, ()


# Every expression a live key interpolates, and whether two gestures of the same
# kind produce the same value for it.
#
# Fail closed by construction: an expression that is not listed fails
# `test_every_key_interpolation_is_classified`, so a new uniquifier spelling
# cannot slip in as "probably stable". That matters because the list is not
# guessable -- `dropSeq`, `tempId`, `batchId` and `++this._referenceMutationSeq`
# are four different ways of writing `Date.now()`.
#
# Expiry: an entry leaves when no key interpolates the expression.
KEY_INTERPOLATIONS = {
    # -- uniquifying: a new value on every call -------------------------------
    "Date.now()": (UNIQUIFYING, "wall clock, read per gesture"),
    "dropContext.sceneId": (STABLE, "the scene a drop landed in"),
    "dropSeq": (UNIQUIFYING,
                "`${Date.now().toString(36)}-${Math.random()...}` minted per drop "
                "in `_handleAssetDrop`"),
    "tempId": (UNIQUIFYING,
               "`temp-queue-${Date.now()...}-${Math.random()...}` minted per "
               "queue addition"),
    "batchId": (UNIQUIFYING, "`crypto.randomUUID()` minted per batch"),
    "++this._referenceMutationSeq": (UNIQUIFYING,
                                     "pre-incremented counter on the widget"),
    # -- stable: the same gesture on the same target repeats it ---------------
    "this.activeSceneId": (STABLE, "the scene being edited"),
    "host.activeSceneId": (STABLE, "the scene being edited, from a panel host"),
    "sceneId": (STABLE, "the scene being edited"),
    "entry.sceneId": (STABLE, "the scene a history entry belongs to"),
    "scene.scene_id": (STABLE, "a scene record's durable id"),
    "projectId": (STABLE, "the project being edited"),
    "laneType": (STABLE, "a lane family name from `lane_registry.js`"),
    "laneIndex": (STABLE, "a lane's position"),
    "type": (STABLE, "an item type name"),
    "id": (STABLE, "an item's durable id or positional address"),
    "clipId": (STABLE, "a clip's durable id"),
    "clip.clip_id": (STABLE, "a clip's durable id"),
    "track.track_id": (STABLE, "an audio track's durable id"),
    "item.reference_item_id": (STABLE, "a Reference item's durable id"),
    "referenceItemId": (STABLE, "a Reference item's durable id"),
    "before.prompt_id": (STABLE, "a prompt section's durable id"),
    "groupId": (STABLE, "a link group's durable id"),
    "fromLaneId": (STABLE, "a Reference lane's durable id"),
    "guide.frame_index": (STABLE, "a guide's frame, which is its address"),
    "this.playhead": (STABLE, "the playhead frame; two gestures at one frame "
                              "address the same target"),
    "idx": (STABLE, "a prompt section's list index"),
    "oldIdx": (STABLE, "the guide frame a move starts from"),
    "keySuffix": (STABLE, "a literal discriminator -- \"clip\", \"audio\", "
                          "\"driver\" -- passed by `_handleAssetDrop`"),
    "selectionKey": (STABLE, "the sorted `type:id` set a mute toggle covers. "
                             "Repeats for two toggles over the same selection -- "
                             "one edit -- and differs for a different selection, "
                             "which is a second edit owing its own undo entry"),
    "laneKey": (STABLE, "the sorted `laneType:laneIndex` set a lane-header "
                        "visibility gesture touches. Repeats for two clicks on "
                        "the same lane -- which is one edit -- and differs for "
                        "two lanes, which are two edits and must keep two undo "
                        "entries"),
    "fieldNames[0]": (STABLE, "the single field name a property write sets"),
    "fieldNames.join(\"-\")": (STABLE, "the sorted field names a property write sets"),
    "operations.map((op) => `${op.lane_type}:${op.lane_index}`).join(\",\")":
        (STABLE, "the lane set the gesture deletes"),
}


def _key_repeats(interpolations: tuple) -> bool:
    return all(KEY_INTERPOLATIONS.get(expression, (UNIQUIFYING, ""))[0] == STABLE
               for expression in interpolations)


def _coalescing_opt_out_findings(sites=None) -> dict:
    """Every enqueue that will not coalesce, and which way it says so."""
    findings = {}
    for site in (sites if sites is not None else _enqueue_call_sites()):
        if site["scope"] in _FORWARDING_SCOPES:
            continue
        shape = site["key_shape"]
        key = f"{site['module']}:{site['scope']}:{shape}"
        if shape in (_KEY_UNRESOLVED, "<unterminated>"):
            findings[key] = (_KEY_UNRESOLVED,
                             "the key cannot be read, so neither tripwire can "
                             "classify this site")
            continue
        if not _key_repeats(site["key_interpolations"]):
            findings[key] = (UNREACHABLE, "the key interpolates "
                             + ", ".join(
                                 expression for expression in site["key_interpolations"]
                                 if KEY_INTERPOLATIONS.get(
                                     expression, (UNIQUIFYING, ""))[0] != STABLE))
            continue
        # Before the `coalesces` check, because that one reads an unreadable
        # `coalesce` as `true` and would file a caller's refusal as consent.
        if site["coalesce_is_caller_supplied"]:
            findings[key] = (CALLER_SUPPLIED,
                             "`coalesce` is passed through from the caller, so "
                             "the decision is not at this enqueue")
            continue
        if not site["coalesces"]:
            findings[key] = (DECLINED, "`coalesce: false` on a key that repeats")
    return findings


# Every enqueue that will not coalesce, with the traced reason it owes.
#
# `UNREACHABLE` -- the key cannot repeat, so the `coalesce: false` beside it is
# redundant and the real decision is the key. The reason answers "why must two
# of these never be one write".
#
# `DECLINED` -- the key repeats and the flag refuses a merge that was available.
# The reason answers "what would a merge lose", and where umbrella Phase C owns
# the answer it names the landing, so an entry that is waiting on work says so
# rather than reading as settled.
#
# Not a permission list: a site here is still a site paying a full document
# write per gesture. `sonder_editor_bugs.md` measures that at 24.2 s for six
# clicks and 460 s for one burst.
#
# Expiry: an entry leaves when its site coalesces, or when its key shape
# changes -- the key IS the dict key, so a reworded key kills the entry and the
# staleness test reports it.
COALESCE_OPT_OUT_REVIEWED = {

    # -- the key cannot repeat -----------------------------------------------

    "editor_prompt_panel.js:commit:scene:${}:prompt-context:${}": (
        UNREACHABLE,
        "the Prompt panel's own dispatcher. Each commit carries a whole "
        "prompt-context document built from the panel's draft, and two drafts "
        "are two documents. A sibling surface umbrella Phase C leaves out of "
        "scope."),
    "editor_reference_panel.js:writeItem:scene:${}:reference-panel:${}:${}": (
        UNREACHABLE,
        "the Reference panel's own dispatcher; the key already names the item, "
        "so the clock is what keeps two edits of ONE item apart. Sibling "
        "surface, out of umbrella Phase C's scope."),
    "editor_reference_panel.js:runItemOperation:scene:${}:reference-panel-op:${}": (
        UNREACHABLE,
        "the Reference panel's own dispatcher, operations built outside the "
        "scope. Sibling surface, out of umbrella Phase C's scope."),
    "editor_widget.js:_mutateReferences:references:${}": (
        UNREACHABLE,
        "the project-level Reference dispatcher. The counter gives every "
        "mutation its own slot so an entity create and a later member write "
        "are never merged. Sibling surface, out of umbrella Phase C's scope."),
    "editor_widget.js:_queueSelectedPromptSectionsWithinGesture:project:queue:prompt-sections:${}": (
        UNREACHABLE,
        "the render-queue dispatcher. Each batch is a distinct set of jobs; "
        "merging two would enqueue one. Sibling surface."),
    "editor_widget.js:_addToRenderQueueWithinGesture:project:queue:add:${}": (
        UNREACHABLE,
        "the render-queue dispatcher; each addition is its own job. Sibling "
        "surface."),
    "editor_widget.js:_addBatchToRenderQueueWithinGesture:project:queue:batch:${}": (
        UNREACHABLE,
        "the render-queue dispatcher; each batch is its own set of jobs. "
        "Sibling surface."),

    "editor_widget.js:_appendReferenceMembersWithinGesture:scene:${}:reference-append:${}:${}": (
        UNREACHABLE,
        "each append sends the WHOLE new member list as `fields.members`, guarded by "
        "`expected: { members: priorMembers }` read from the reconciled scene. "
        "The gesture has no local apply and does not touch `item.members`, so "
        "the two appends chain through the server round trip rather than "
        "through local state -- which is exactly what the uniquified key "
        "preserves. Two appends are two additions and neither may be dropped. "
        "(Umbrella Phase C §3 Class C keeps it that way: its Reference local "
        "apply is geometry-only precisely so that lengthening `item.members` "
        "optimistically cannot turn the second append into a 409.)"),
    "editor_widget.js:_placeReferencePayloadWithinGesture:scene:${}:reference-stage:${}": (
        UNREACHABLE,
        "`create_reference_item` creates a row, alongside the lane it needs. "
        "Two placements are two items."),
    "editor_widget.js:_handleAssetDropWithinGesture:scene:${}:drop:${}:${}": (
        UNREACHABLE,
        "`create_clip` / `create_audio_track` / `create_guide`: each drop "
        "imports its own media. `dropSeq` separates one drop from the next and "
        "`keySuffix` separates the several writes WITHIN a drop, which is why "
        "the same scope holds two shapes."),
    "editor_widget.js:_handleAssetDropWithinGesture:scene:${}:drop:${}:guide": (
        UNREACHABLE,
        "the guide half of the same drop gesture; see the clip/audio shape "
        "above."),
    "editor_widget.js:_saveNewPromptSectionWithinGesture:prompt:${}:create:${}": (
        UNREACHABLE,
        "`_apply_create_prompt_section` creates a row; two creates are two "
        "sections."),
    "editor_widget.js:_applyPromptSetupWithinGesture:prompt:${}:apply:${}": (
        UNREACHABLE,
        "`_apply_replace_prompt_sections` replaces the whole collection behind "
        "`_validate_prompt_replacement_identity`, whose `expected` describes "
        "the collection the author replaced, and the batch prepends "
        "`create_prompt_semantic_unit` intents that are consumed once. It also "
        "carries `retryOnConflict: false`, which "
        "`test_a_caller_that_declines_a_retry_also_declines_coalescing` "
        "requires be paired with a coalescing refusal."),
    "editor_widget.js:_createLinkGroupFromSelectionWithinGesture:scene:${}:link-items:${}": (
        UNREACHABLE,
        "`_add_link_group` runs `_unlink_refs` first, so two link gestures are "
        "not two assignments to one row."),
    "editor_widget.js:_unlinkSelectedItemsWithinGesture:scene:${}:unlink-items:${}": (
        UNREACHABLE,
        "`_unlink_refs` then `_prune_linked_item_groups`; a second gesture's "
        "refs are only meaningful against the groups the first one left."),
    "editor_widget.js:_deleteSelectedItemsWithinGesture:scene:${}:delete-selected:${}": (
        UNREACHABLE,
        "`_apply_bulk_delete_items` removes the rows `items` names; a second "
        "gesture names the rows the first left, so replacement would drop one "
        "delete while telling its author it worked."),
    "editor_widget.js:_moveItemToNewLaneWithinGesture:scene:${}:move-item-new-lane:${}": (
        UNREACHABLE,
        "the key names no item, so a stable one would let two moves of "
        "DIFFERENT items collide; and the gesture sends `set_lane_count` with "
        "an absolute computed from the local scene. Umbrella Phase C stage 2 L6 "
        "gives it a local apply and does not change the key."),
    "editor_widget.js:_consolidateSelectedItemsToLaneWithinGesture:scene:${}:consolidate:${}:${}": (
        UNREACHABLE,
        "`_consolidate_media_items` compacts the lanes it empties and returns "
        "`final_target_lane`, so a second consolidation's `target_lane` means "
        "something only after the first has been applied. Umbrella Phase C "
        "stage 2 L6 gives it a local apply and does not change the key."),
    "editor_widget.js:_moveReferenceLaneWithinGesture:scene:${}:reference-move-lane:${}:${}": (
        UNREACHABLE,
        "`_move_media_lane` reorders by from/to index, so a second move's "
        "indices describe the order the first produced."),
    "editor_widget.js:_commitItemMove:scene:${}:move-commit:${}": (
        UNREACHABLE,
        "a drag commit emits one operation per moved item across arbitrary "
        "lanes and types, and the key names the gesture rather than any item -- "
        "so a stable key would let one drag's commit replace another's."),
    "editor_widget.js:_commitTrim:scene:${}:trim-commit:${}": (
        UNREACHABLE,
        "a trim commit has the same shape as a drag commit: one operation per "
        "trimmed item, a key that names no item."),
    "editor_widget.js:_splitItemsAtFrameWithinGesture:scene:${}:split:${}": (
        UNREACHABLE,
        "`_apply_split_linked` cuts at a frame inside the item's CURRENT "
        "bounds, which the previous cut changed, so two cuts are never one "
        "write. Phase C stage 2 L3 batched every target of one gesture into "
        "a single operations array, so the key no longer names an item -- it "
        "names the gesture, and only the clock keeps two apart. The explicit "
        "`coalesce: false` states that decision rather than creating it."),
    "editor_widget.js:_replaceClipSource:clip:${}:replace-source:${}": (
        UNREACHABLE,
        "`_apply_replace_clip_source` re-derives bounds from the new asset's "
        "length, so two replacements are not two assignments to one row."),
    "editor_widget.js:_replaceAudioSource:audio:${}:replace-source:${}": (
        UNREACHABLE,
        "`_apply_replace_audio_source` is the audio twin; same reasoning."),
    "editor_widget.js:_runUndoWithinGesture:prompt:${}:identity-cleanup:${}": (
        UNREACHABLE,
        "a history-internal identity write naming one entry's semantic-unit "
        "cleanup, consumed once. Merging two would leave one entry's units "
        "unreconciled."),
    "editor_widget.js:_finalizeCommittedHistoryAmbiguityWithinGesture:prompt:${}:identity-cleanup:${}": (
        UNREACHABLE,
        "the same identity cleanup, reached when a history ambiguity is "
        "resolved as committed."),
    "editor_widget.js:_finalizeRefusedHistoryAmbiguityWithinGesture:prompt:${}:identity-redo-refused:${}": (
        UNREACHABLE,
        "the same identity cleanup, reached when a history ambiguity is "
        "resolved as refused."),
    "editor_widget.js:_runRedoWithinGesture:prompt:${}:identity-redo:${}": (
        UNREACHABLE,
        "a history-internal identity write for one redo, consumed once."),
    "editor_widget.js:_runRedoWithinGesture:prompt:${}:identity-redo-compensation:${}": (
        UNREACHABLE,
        "the compensating identity write for one redo, consumed once."),

    # -- the key repeats and the flag refuses a merge -------------------------
    "editor_widget.js:_updateItemPropertyWithinGesture:${}:${}:field:${} | ${}:${}:fields:${}": (
        CALLER_SUPPLIED,
        "the only gesture whose coalescing is decided by its CALLERS: it takes "
        "`coalesce` in its options bag and passes it through as shorthand, so "
        "neither the flag nor the key answers the question here. Most callers "
        "take the default and coalesce behind an effective merge. One declines "
        "-- the Reference strength input in the item editor -- with a traced "
        "reason beside it: the guard is built from the unmutated item and a "
        "Reference `identity_mismatch` is terminal, so a merged burst would "
        "settle every collapsed author from one refusal. Filed here rather than "
        "as DECLINED because a refusal that lives in the callers is a different "
        "fact from one stated at the enqueue, and reading the shorthand as "
        "`true` hid it from both tripwires."),


    "editor_widget.js:_updateSceneGlobalContextWithinGesture:scene:${}:global_prompt_context": (
        DECLINED,
        "already coalesced ONE LAYER UP, which is why umbrella Phase C stage 1 "
        "L3 left it alone rather than merging it. Every live caller reaches it "
        "through `savePromptDraft` (`web/js/prompt_edit_intent.js`), which "
        "single-flights per draft key -- `if (row.pending) { row.saveAgain = "
        "true; return row.pending; }` -- and then re-saves the freshest draft "
        "in its own loop. A burst of typing is therefore already one write in "
        "flight plus one more carrying the merged latest value, which is "
        "exactly what queue coalescing would produce; and the draft layer also "
        "owns the baseline reconciliation (`mergePromptRecords`) that the queue "
        "cannot do. `commitGlobalScope` in `editor_prompt_panel.js` is declared "
        "and never called, so it is not a second path. Coalescing here would "
        "add no write saving and two hazards: `fields.prompt_edit` is a "
        "per-record delta the shared merge preserves rather than folds, and the "
        "gesture carries its own revision-token rollback that selects the "
        "NEWEST author where a coalesced group needs the oldest -- two "
        "head-ness mechanisms for no gain."),
    "editor_widget.js:_updateSceneFpsWithinGesture:scene:${}:fps": (
        DECLINED,
        "`_fpsUpdatePending` already serialises fps by DROPPING a second "
        "gesture and re-syncing the control, so nothing accumulates behind this "
        "key and there is nothing to coalesce. Umbrella Phase C stage 1 L3 TOOK "
        "option (a) -- keep the drop -- and the reason is stronger than the "
        "plan's: retiming is applied entirely from the response by "
        "`reconcileRetimedScene`, using a `scale` and a `previousState` "
        "captured before the write. That is client state the queue never sees "
        "and a merge could not recompute for a collapsed group, so coalescing "
        "fps is not merely unnecessary, it is not expressible. Whether "
        "drop-and-resync is right behaviour at all is a separate UX question "
        "the plan hands on, and it is NOT settled here."),

    "editor_widget.js:_moveItemToFrameWithinGesture:${}:${}:timeline": (
        DECLINED,
        "the Reference branch sends `expected: { start_frame, end_frame }` read "
        "from the live scene record -- a before-value claim. The gesture writes "
        "no optimistic state today, so two of them read the same values and a "
        "merge would be safe by accident rather than by design; umbrella Phase "
        "C stage 2 L6 adds the local apply that makes the oldest-`expected` "
        "merge load-bearing. Declined until that landing supplies one."),
    "editor_widget.js:_addLaneWithinGesture:scene:${}:${}-lane-count": (
        DECLINED,
        "`set_lane_count` carries an absolute, and two authored lane additions "
        "would also collapse into ONE undo entry -- `willCoalesce` discards the "
        "superseded gesture's entry in `_queueProjectMutation`. One Ctrl+Z per "
        "authored gesture is the reason to decline here, not the payload."),
    "editor_widget.js:_removeLaneWithinGesture:scene:${}:${}-remove-lane:${}": (
        DECLINED,
        "`_remove_media_lane` shifts every lane above the one it removes, so a "
        "second removal at the same index names a DIFFERENT lane. The key "
        "carries the index, which is exactly the value the first removal "
        "invalidates."),
    "editor_widget.js:_removeLaneWithItemsWithinGesture:scene:${}:${}-remove-lane:${}": (
        DECLINED,
        "the delete-the-items variant of the removal above, and the same lane "
        "renumbering applies. Its key shape matches, which is why the scope is "
        "part of the entry key."),
    "editor_widget.js:_removeLaneDeletingItemsWithinGesture:scene:${}:${}-delete-lane-and-items:${}": (
        DECLINED,
        "a third removal entry point; `_remove_media_lane` renumbers the lanes "
        "above it just the same."),
    "editor_widget.js:_deleteSelectedLanesAndItemsWithinGesture:scene:${}:delete-selected-lanes:${}": (
        DECLINED,
        "the same lane renumbering over a SET: the key lists the lane types and "
        "indices the gesture deletes, and the first deletion renumbers them."),
    "editor_widget.js:_deleteItemsInLaneWithinGesture:scene:${}:${}-delete-lane-items:${}": (
        DECLINED,
        "`_apply_bulk_delete_items` names the rows it removes; a second gesture "
        "on the same lane names the rows the first left, so a replacement drops "
        "one delete while telling its author it worked."),
    "editor_widget.js:_moveGuideToFrameWithinGesture:guide:${}:${}:move": (
        DECLINED,
        "`_apply_move_guide` is addressed by `from_frame_index`, which the "
        "previous move changed -- and the key carries that index."),
    "editor_widget.js:_addClipFrameToGuidesWithinGesture:guide:${}:${}:create": (
        DECLINED,
        "`_apply_create_guide` creates a row from a CLIENT-minted `guide_id`, "
        "behind a `_guideReplacementGuard` describing what occupied the frame. "
        "Two creates at one playhead mint two ids, and a replacement discards "
        "one while telling its author it worked."),
    "editor_widget.js:_showGuideManagementPopup:guide:${}:${}:delete": (
        DECLINED,
        "`_apply_delete_guide` is consumed once and carries an identity "
        "`expected` including `guide_id`, so a second delete at the same frame "
        "describes a guide the first one removed."),
    "editor_widget.js:_showGuideManagementPopupLegacy:guide:${}:${}:delete": (
        DECLINED,
        "the legacy popup's copy of the same delete; same reasoning, and the "
        "matching key shape is why the scope is part of the entry key."),
    "editor_widget.js:_updatePromptSectionWithinGesture:prompt:${}:${}:fields": (
        DECLINED,
        "`_apply_update_prompt_section` is addressed by LIST INDEX, and "
        "`_split_prompt_object` re-sorts `prompt_sections` while "
        "`_apply_swap_prompt_sections` reorders them; its `fields` can also "
        "carry `channels` / `channel_docs`, which the server applies key by "
        "key."),
    "editor_widget.js:_deletePromptSectionWithinGesture:prompt:${}:${}:delete": (
        DECLINED,
        "`_apply_delete_prompt_section` is addressed by list index and the key "
        "carries that index, which the first deletion renumbers."),
    "editor_widget.js:_updateLinkedPromptAttachmentWithinGesture:prompt:${}:linked:${}": (
        DECLINED,
        "carries `retryOnConflict: false` as a stated caller override, and "
        "`test_a_caller_that_declines_a_retry_also_declines_coalescing` "
        "requires the pair: the override lives in the `run` closure, which a "
        "joining gesture replaces."),
    "editor_widget.js:_queuePromptProjectWrite:prompt-project:${}": (
        DECLINED,
        "not a scene mutation at all -- `run` PUTs the whole project, and the "
        "intent is a `structuredClone` of the caller's body, so a replaced "
        "intent loses every field the newer body does not carry. The scene "
        "dispatcher's merge vocabulary does not apply to it."),
}


def test_every_key_interpolation_is_classified():
    """A new uniquifier spelling must not read as "probably stable".

    The classification is not guessable -- `dropSeq`, `tempId`, `batchId` and
    `++this._referenceMutationSeq` are four different ways of writing
    `Date.now()`, and each is used exactly once or twice. An unlisted
    expression therefore fails here rather than defaulting, and the default it
    would otherwise take (`UNIQUIFYING`) is itself the fail-closed direction.
    """
    seen = set()
    for site in _enqueue_call_sites():
        if site["scope"] in _FORWARDING_SCOPES:
            continue
        seen.update(site["key_interpolations"])
    unknown = sorted(seen - set(KEY_INTERPOLATIONS))
    assert not unknown, (
        "a mutation key interpolates expressions nobody has classified: "
        f"{unknown}. Say whether each one repeats across two gestures of the "
        "same kind (STABLE) or changes on every call (UNIQUIFYING). A "
        "uniquified key defeats coalescing exactly as well as "
        "`coalesce: false` and is invisible to the flag scan.")
    dead = sorted(set(KEY_INTERPOLATIONS) - seen)
    assert not dead, (
        f"no mutation key interpolates these any more, so the entries are "
        f"documentation of nothing: {dead}")


def test_every_enqueue_that_cannot_coalesce_says_why():
    """The tripwire this landing exists for, in both its spellings.

    A site reaches here because it declared `coalesce: false` on a key that
    repeats, or because its key can never repeat. Either way it pays the full
    route floor once per gesture, and the reason has to be traced -- the
    umbrella's whole complaint is about defaults nobody chose.
    """
    findings = _coalescing_opt_out_findings()
    unreviewed = {key: why for key, why in findings.items()
                  if key not in COALESCE_OPT_OUT_REVIEWED}
    assert not unreviewed, (
        "these enqueues will not coalesce and no one has said why. Each gesture "
        "therefore costs a whole document write -- sonder_editor_bugs.md "
        "measures six clicks at 24.2 s and one burst at 460 s. Add an entry to "
        "COALESCE_OPT_OUT_REVIEWED: UNREACHABLE with the reason two of these "
        "must never be one write, or DECLINED with what a merge would lose. "
        "Trace the reason -- it is a claim about code: "
        + "; ".join(f"{key} -- {why[0]}: {why[1]}"
                    for key, why in sorted(unreviewed.items())))


def test_an_opt_out_entry_describes_the_spelling_it_is_filed_under():
    """A `DECLINED` reason answers a different question from an `UNREACHABLE` one.

    Filing a uniquified key as `DECLINED` would claim a merge was refused when
    none was ever possible, and filing a real refusal as `UNREACHABLE` would
    hide the decision behind the key. The classes are not interchangeable and
    the scan knows which is which, so it checks rather than trusts.
    """
    findings = _coalescing_opt_out_findings()
    wrong = {key: (entry[0], findings[key][0])
             for key, entry in COALESCE_OPT_OUT_REVIEWED.items()
             if key in findings and entry[0] != findings[key][0]}
    assert not wrong, (
        "these entries are filed under the wrong spelling (entry, actual): "
        f"{wrong}")
    assert not [key for key, entry in COALESCE_OPT_OUT_REVIEWED.items()
                if entry[0] not in (DECLINED, UNREACHABLE, CALLER_SUPPLIED)], (
        "an entry declares a class that is none of DECLINED, UNREACHABLE "
        "or CALLER_SUPPLIED")
    for key, (_, reason) in COALESCE_OPT_OUT_REVIEWED.items():
        assert len(reason) > 40, (
            f"{key} carries a reason too short to be a traced one: {reason!r}")


def test_opt_out_entries_are_not_stale():
    """An entry must be able to die, or it is documentation of a gap.

    The entry key is the KEY SHAPE, so this fires when a site starts coalescing
    AND when its key is reworded -- which is the point. A reworded key is a new
    decision about whether two gestures are one edit, and the old reason
    described the old key.
    """
    findings = _coalescing_opt_out_findings()
    gone = sorted(set(COALESCE_OPT_OUT_REVIEWED) - set(findings))
    assert not gone, (
        "these enqueues now coalesce, or their key shape changed, so the entry "
        f"describes something that is no longer there: {gone}")


def test_an_unreadable_key_can_never_be_reviewed():
    """Fail closed: an entry cannot buy a pass for a key the scan cannot read.

    `_KEY_UNRESOLVED` is not one of the two classes an entry may declare, so a
    site whose key resolves to nothing stays a finding no matter what is filed
    for it. Without this, the cheapest repair for an unreadable key would be to
    file it rather than to spell it.
    """
    assert _KEY_UNRESOLVED not in (DECLINED, UNREACHABLE, CALLER_SUPPLIED)
    filed = {key for key, entry in COALESCE_OPT_OUT_REVIEWED.items()
             if entry[0] == _KEY_UNRESOLVED}
    assert not filed, f"an entry claims the unresolved class: {sorted(filed)}"
    findings = _coalescing_opt_out_findings()
    unreadable = sorted(key for key, why in findings.items()
                        if why[0] == _KEY_UNRESOLVED)
    assert not unreadable, (
        "these enqueues state a key the scanner cannot read, so neither "
        f"coalescing tripwire can classify them: {unreadable}. Spell the key as "
        "a template literal at the call site, or as a `const` in the same "
        "method.")


def test_the_opt_out_scan_sees_both_spellings():
    """Liveness. A tripwire that finds nothing has never been observed to work.

    Pinned rather than bounded, per this module's convention: the split between
    the two spellings is the finding that justified building two tripwires
    instead of one, and it should move only deliberately.
    """
    findings = _coalescing_opt_out_findings()
    declined = [key for key, why in findings.items() if why[0] == DECLINED]
    unreachable = [key for key, why in findings.items() if why[0] == UNREACHABLE]
    assert len(declined) == EXPECTED_STABLE_KEY_OPT_OUTS, (
        f"stable-key opt-outs moved: {len(declined)} found, "
        f"{EXPECTED_STABLE_KEY_OPT_OUTS} pinned")
    assert len(unreachable) == EXPECTED_UNIQUIFIED_KEY_OPT_OUTS, (
        f"uniquified-key opt-outs moved: {len(unreachable)} found, "
        f"{EXPECTED_UNIQUIFIED_KEY_OPT_OUTS} pinned")
    caller_supplied = [key for key, why in findings.items()
                       if why[0] == CALLER_SUPPLIED]
    assert len(caller_supplied) == EXPECTED_CALLER_SUPPLIED_OPT_OUTS, (
        f"caller-supplied coalescing moved: {len(caller_supplied)} found, "
        f"{EXPECTED_CALLER_SUPPLIED_OPT_OUTS} pinned")
    # The remaining measured entry must be present and filed as a real refusal,
    # or the tripwire is passing it for the wrong reason. Lane-header visibility
    # visibility, selected mute and clip-role conversion all coalesce now, which
    # is why `EXPECTED_STABLE_KEY_OPT_OUTS` went 20 -> 17 across stage 1: a count
    # that only ever grows would not be a ratchet.
    for retired in ("_applyHeaderVisibilityBulkWithinGesture",
                    "_toggleSelectedMuteWithinGesture",
                    "_convertClipRoleWithinGesture"):
        assert not any(retired in key for key in findings), (
            f"{retired} coalesces now; an entry for it would be stale")
    # `_moveItemToFrameWithinGesture` is the surviving DECLINED entry umbrella
    # Phase C still owes an answer for -- stage 2 L6 gives it the local apply
    # that makes its oldest-`expected` merge load-bearing.
    assert any("_moveItemToFrameWithinGesture" in key for key in declined)
    # And every site that owns a decision must have produced a readable key.
    # The forwarding helpers are excluded for the same reason they are excluded
    # from the findings: `_runSceneMutation` spells its key `key || <default>`,
    # which is plumbing for whatever its caller passed rather than a key of its
    # own, and reading it would report the receiver instead of the decision.
    for site in _enqueue_call_sites():
        if site["scope"] in _FORWARDING_SCOPES:
            continue
        assert site["key_shape"] not in (_KEY_UNRESOLVED, "<unterminated>"), (
            f"{site['module']}:{site['scope']}:{site['line']} states a key the "
            "scanner cannot read")


def test_a_coalescing_site_is_not_reported_as_an_opt_out():
    """The five live coalescing gestures must clear both tripwires.

    They have stable keys and do not pass `coalesce: false`, so any of them
    appearing as a finding means the key reader or the flag reader has broken
    in a way the other tests would not notice.
    """
    findings = _coalescing_opt_out_findings()
    for scope in ("_updateSceneResolutionWithinGesture", "_renameSceneWithinGesture",
                  "_updateSceneDurationWithinGesture", "_saveLaneConfigWithinGesture"):
        assert not [key for key in findings if f":{scope}:" in key], (
            f"{scope} coalesces on a repeating key and must not read as an opt-out")
    # `_updateItemPropertyWithinGesture` is deliberately NOT in that list. It
    # was, and the assertion pinned a hole shut: the gesture passes `coalesce`
    # through from its callers, one of which declines on a stable key, and
    # asserting it could never be a finding is what kept that invisible.
    assert [key for key, why in findings.items()
            if ":_updateItemPropertyWithinGesture:" in key
            and why[0] == CALLER_SUPPLIED], (
        "the pass-through gesture must read as caller-supplied, or the third "
        "spelling of `will not coalesce` is unwatched again")


def test_the_opt_out_tripwires_produce_the_findings_they_claim():
    """Drives both predicates over fixtures, with exact findings.

    Without this the real modules decide whether the tripwire has ever been
    exercised at all, and both assertions above would be satisfied by a
    predicate that returns the same thing for every input.
    """
    def findings(js):
        return _coalescing_opt_out_findings(
            _scan_enqueue_sites((("fixture.js", js),)))

    declined = findings(
        'async function gesture() {\n'
        '  await host._runSceneMutation(ops, { key: `scene:${sceneId}:x`,\n'
        '    coalesce: false });\n}\n')
    assert list(declined) == ["fixture.js:gesture:scene:${}:x"]
    assert declined["fixture.js:gesture:scene:${}:x"][0] == DECLINED

    unreachable = findings(
        'async function gesture() {\n'
        '  await host._runSceneMutation(ops, { key: `scene:${sceneId}:x:${Date.now()}`,\n'
        '    coalesce: true });\n}\n')
    assert list(unreachable) == ["fixture.js:gesture:scene:${}:x:${}"]
    assert unreachable["fixture.js:gesture:scene:${}:x:${}"][0] == UNREACHABLE, (
        "a uniquified key must be a finding even when the flag says `true` -- "
        "that is the whole reason there are two tripwires")

    assert not findings(
        'async function gesture() {\n'
        '  await host._runSceneMutation(ops, { key: `scene:${sceneId}:x` });\n}\n'), (
        "a coalescing gesture on a repeating key is not an opt-out")

    assert not findings(
        'async function gesture() {\n'
        '  await host._runSceneMutation(ops, { label: "no key" });\n}\n'), (
        "an absent key is `_runSceneMutation`'s own scene-scoped default, which "
        "repeats; reading it as unresolved would report the most coalescible "
        "key in the file as unclassifiable")

    nested = findings(
        'async function gesture() {\n'
        '  await host._runSceneMutation(ops, {\n'
        '    key: `scene:${sceneId}:lanes:${ops.map((op) => `${op.lane_index}`).join(",")}`,\n'
        '    coalesce: false });\n}\n')
    assert list(nested) == ["fixture.js:gesture:scene:${}:lanes:${}"], (
        "a nested template literal inside an interpolation must not cut the key "
        "in half -- one live key is spelled exactly this way")

    ternary = findings(
        'async function gesture() {\n'
        '  const key = names.length === 1\n'
        '    ? `${type}:${id}:field:${names[0]}`\n'
        '    : `${type}:${id}:fields:${Date.now()}`;\n'
        '  await host._runSceneMutation(ops, { key, coalesce: true });\n}\n')
    assert list(ternary) == ["fixture.js:gesture:${}:${}:field:${} | ${}:${}:fields:${}"], (
        "both branches of a ternary key must be read; taking only the first "
        "would classify the site on half its evidence")


def test_the_key_reader_is_not_fooled_by_the_operations_argument():
    """A `key` field inside an operation is not this enqueue's key.

    The first argument is a list of operations and an operation may carry a
    field called `key`; a plain search takes the first match and files the site
    under a key that does not exist -- silently, because the shape it records
    still looks like one. The reader walks the call instead and accepts only
    array depth 0, brace depth 1.
    """
    findings = _coalescing_opt_out_findings(_scan_enqueue_sites((("fixture.js",
        'async function gesture() {\n'
        '  await host._runSceneMutation(\n'
        '    [{ type: "update_scene_fields", fields: { key: "decoy" } }],\n'
        '    { key: `scene:${sceneId}:real`, coalesce: false });\n}\n'),)))
    assert list(findings) == ["fixture.js:gesture:scene:${}:real"], (
        "the decoy inside the operations argument was read as the enqueue's key")


def test_a_spread_options_object_is_unreadable_rather_than_the_default():
    """An absent key means the default; a spread means the scan cannot see it.

    Reading a spread as "no key stated" reports the most coalescible key in the
    file for a site whose real key it never saw, and `_KEY_UNRESOLVED` is not a
    class an entry may claim -- so this fails the suite until the key is spelled
    where the scan can read it.
    """
    findings = _coalescing_opt_out_findings(_scan_enqueue_sites((("fixture.js",
        'async function gesture() {\n'
        '  await host._runSceneMutation(ops, { ...options, coalesce: false });\n}\n'),)))
    assert [why[0] for why in findings.values()] == [_KEY_UNRESOLVED]


def test_a_caller_supplied_coalesce_is_its_own_answer():
    """Neither `true` nor `false`: the decision is in the callers.

    Reading shorthand as `true` is what hid the live Reference-strength refusal
    from both tripwires, so the fixture pins all three spellings apart.
    """
    def classes(js):
        return sorted(why[0] for why in _coalescing_opt_out_findings(
            _scan_enqueue_sites((("fixture.js", js),))).values())

    assert classes(
        'async function gesture() {\n'
        '  await host._runSceneMutation(ops, { key: `scene:${sceneId}:x`, coalesce });\n}\n'
    ) == [CALLER_SUPPLIED]
    assert classes(
        'async function gesture() {\n'
        '  await host._runSceneMutation(ops, { key: `scene:${sceneId}:x`,\n'
        '    coalesce: opts.coalesce });\n}\n'
    ) == [CALLER_SUPPLIED]
    assert classes(
        'async function gesture() {\n'
        '  await host._runSceneMutation(ops, { key: `scene:${sceneId}:x`,\n'
        '    coalesce: true });\n}\n'
    ) == []
    assert classes(
        'async function gesture() {\n'
        '  await host._runSceneMutation(ops, { key: `scene:${sceneId}:x`,\n'
        '    coalesce: false });\n}\n'
    ) == [DECLINED]


# Every shape an exported merge can take, and what each must resolve to. The
# first row is the one that matters most: a concise arrow ignoring its older
# argument is `merge: (a, b) => b` wearing a badge, and an earlier version of
# the resolver PASSED it -- searching forward for a `{` found an unrelated
# block later in the module and ran the first-parameter check against that.
# Failing open is the one outcome this tripwire must never have.
IMPORTED_MERGE_SHAPES = [
    pytest.param("export const probeMerge = (a, b) => b;\n"
                 "function later(older, newer) { return older + newer; }\n",
                 False, id="concise-arrow-ignores-older"),
    pytest.param("export const probeMerge = (older, newer) => older.concat(newer);\n",
                 True, id="concise-arrow-reads-older"),
    pytest.param("export const probeMerge = (older, newer) => {\n"
                 "    return { ...newer, ...older };\n};\n",
                 True, id="block-arrow-reads-older"),
    pytest.param("export function probeMerge(older, newer = null) {\n"
                 "    return older || newer;\n}\n",
                 True, id="declared-function-with-a-default"),
    pytest.param("export function probeMerge(older, newer) {\n"
                 "    return newer;\n}\n",
                 False, id="declared-function-ignores-older"),
    pytest.param("export function probeMerge(only) {\n    return only;\n}\n"
                 "function later(older, newer) { return older; }\n",
                 False, id="one-parameter"),
    pytest.param("export const probeMerge = ({ older, newer }) => older;\n",
                 False, id="destructured-is-unreadable-so-fails-closed"),
]


@pytest.mark.parametrize("module_body,effective", IMPORTED_MERGE_SHAPES)
def test_an_imported_merge_resolves_to_its_own_definition(
        module_body, effective, tmp_path, monkeypatch):
    """Follow the import, and bound the answer to THAT definition.

    `JS_DIR` is redirected rather than writing a probe module into `web/js`,
    which ships in the published pack -- a test that leaves a file there on a
    hard failure would publish it.
    """
    (tmp_path / "zz_probe.js").write_text(module_body, encoding="utf-8")
    monkeypatch.setattr("test_scene_mutation_registration.JS_DIR", tmp_path)
    source = "\n".join([
        'import { probeMerge } from "./zz_probe.js";',
        "async function gesture() {",
        "  await host._runSceneMutation(ops, { key: `scene:${sceneId}:x`,",
        "    coalesce: true, merge: probeMerge });",
        "}",
        "",
    ])
    site = _scan_enqueue_sites((("fixture.js", source),))[0]
    assert site["effective_merge"] is effective


def test_an_aliased_import_resolves_by_its_exported_name(tmp_path, monkeypatch):
    """`{ exported as local }` is looked up one way and matched the other.

    Using the local name for both made every aliased import unresolvable, which
    fails closed but reports the opposite of the truth -- and the cheapest
    repair for that is to stop importing the shared merge.
    """
    (tmp_path / "zz_probe.js").write_text(
        "export const probeMerge = (older, newer) => ({ ...newer, ...older });\n",
        encoding="utf-8")
    monkeypatch.setattr("test_scene_mutation_registration.JS_DIR", tmp_path)
    source = "\n".join([
        'import { probeMerge as myMerge } from "./zz_probe.js";',
        "async function gesture() {",
        "  await host._runSceneMutation(ops, { key: `scene:${sceneId}:x`,",
        "    coalesce: true, merge: myMerge });",
        "}",
        "",
    ])
    assert _scan_enqueue_sites((("fixture.js", source),))[0]["effective_merge"]


def test_a_badge_merge_is_not_certified_by_a_later_function():
    """The merge is whatever comes first after `merge:`.

    Trying the arrow pattern and then falling through to the function pattern
    over the same text let `(a, b) => b` be certified by an unrelated
    two-parameter function further down the extent.
    """
    assert not _reads_its_older_argument(
        "(a, b) => b, failureMessage: function name(older, newer) { return older; }")
    assert _reads_its_older_argument("(older, newer) => ({ ...newer, ...older })")


# ---------------------------------------------------------------------------
# A coalescing site whose operations are built somewhere else
# ---------------------------------------------------------------------------
# The payload checks above resolve operands by ENCLOSING SCOPE. That is right
# for a gesture that builds its own operations, and blind to one that calls a
# helper: `_applyHeaderVisibilityBulkWithinGesture` sends `update_lane_config`
# from its own body and five more operation types from
# `_buildLinkedMuteOperations` -> `_muteOperationForItem`, none of which the
# scan attributes to it. So the sub-keyed and opaque-payload checks are reading
# a fraction of what that site sends, and clearing it on that fraction.
#
# The answer is not to widen the attribution -- a helper's operations belong to
# every caller and attributing them to one would be worse than seeing none. It
# is to notice that the site's payload cannot be read here, and require the
# protection that does not depend on reading it: an effective merge, which
# preserves whatever it cannot classify.

# Captures the leading underscore, because that is part of the scope NAME the
# resolver records. Dropping it made every call miss and the transitive hop
# silently never fire.
_CALL_RE = re.compile(r"\bthis\.(_\w+)\s*\(")


def _operation_emitting_scopes(sources: tuple) -> dict:
    """Per module, the scopes that own an operation literal or reach one."""
    reaching = {}
    for module, source in sources:
        mask = _code_mask(source)
        scopes = _scopes(source, mask)
        bodies = {}
        for name, begin, end in scopes:
            bodies.setdefault(name, set()).update(
                _CALL_RE.findall(_code_only(source[begin:end], mask[begin:end])))
        emitters = {literal["scope"] for literal in _scan(source, module)}
        # The forwarding helpers are the enqueue itself, not a payload builder,
        # and every gesture calls one -- leaving them in made the closure swallow
        # the whole file and reported three whole-value gestures as delegating.
        emitters -= _FORWARDING_SCOPES
        # Transitive: `_buildLinkedMuteOperations` owns no literal itself, it
        # calls `_muteOperationForItem`. One hop would miss it.
        changed = True
        while changed:
            changed = False
            for name, called in bodies.items():
                if name in _FORWARDING_SCOPES:
                    continue        # never re-admitted by the closure either
                if name not in emitters and called & emitters:
                    emitters.add(name)
                    changed = True
        reaching[module] = (emitters, bodies)
    return reaching


def _sites_with_operations_built_elsewhere(sources=None) -> dict:
    """Coalescing sites that call an operation emitter and supply no merge."""
    sources = sources or tuple(
        (module, (JS_DIR / module).read_text(encoding="utf-8"))
        for module in EMITTING_MODULES)
    reaching = _operation_emitting_scopes(sources)
    findings = {}
    for site in _scan_enqueue_sites(sources):
        if site["scope"] in _FORWARDING_SCOPES or not site["coalesces"]:
            continue
        if site["effective_merge"]:
            continue
        emitters, bodies = reaching[site["module"]]
        called = bodies.get(site["scope"], set()) & emitters
        if called:
            findings[f"{site['module']}:{site['scope']}"] = sorted(called)
    return findings


def test_a_coalescing_site_that_builds_its_payload_elsewhere_supplies_a_merge():
    """Because the payload checks cannot see what it sends.

    They resolve operands by enclosing scope, so a gesture that delegates gets
    cleared on the fraction it happens to build inline. An effective merge is
    the protection that does not depend on reading the payload: it preserves
    what it cannot classify.
    """
    findings = _sites_with_operations_built_elsewhere()
    assert not findings, (
        "these coalescing sites build operations in a helper, so the sub-keyed "
        "and opaque-payload checks above are reading only part of what they "
        "send -- and they supply no merge, so wholesale replacement decides "
        f"what survives: {findings}. Pass `coalesceSceneMutationIntents`, or "
        "record the site with a traced reason.")


def test_the_hidden_operand_scan_finds_the_helper_chain_it_was_written_for():
    """Liveness, and the transitive hop specifically.

    `_buildLinkedMuteOperations` owns no operation literal -- it calls
    `_muteOperationForItem`, which does. A one-hop reachability check would
    report the lane-header gesture as building everything inline, which is the
    blindness this test exists to disprove.
    """
    sources = tuple((module, (JS_DIR / module).read_text(encoding="utf-8"))
                    for module in EMITTING_MODULES)
    emitters, bodies = _operation_emitting_scopes(sources)["editor_widget.js"]
    assert "_muteOperationForItem" in emitters, "the direct emitter is not seen"
    assert "_buildLinkedMuteOperations" in emitters, (
        "the transitive hop is not followed, so a gesture that delegates twice "
        "reads as building its own payload")
    assert "_buildLinkedMuteOperations" in bodies["_applyHeaderVisibilityBulkWithinGesture"]

    # And the predicate must produce a finding when the merge goes.
    fixture = (
        'function _muteOperationForItem(item) {\n'
        '  return { type: "update_prompt_section", index: item.id,\n'
        '    fields: { muted: true } };\n}\n'
        'function _buildMutes(items) {\n'
        '  return items.map((item) => this._muteOperationForItem(item));\n}\n'
        'async function gesture() {\n'
        '  const operations = this._buildMutes(items);\n'
        '  await host._runSceneMutation(operations, {\n'
        '    key: `scene:${sceneId}:x`, coalesce: true });\n}\n')
    found = _sites_with_operations_built_elsewhere((("fixture.js", fixture),))
    assert list(found) == ["fixture.js:gesture"], (
        f"the predicate did not fire on a delegating coalescing site: {found}")
