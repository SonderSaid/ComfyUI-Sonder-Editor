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
    "editor_widget.js": 82,
    "editor_prompt_panel.js": 4,
    "editor_reference_panel.js": 3,
    "prompt_context_chips.js": 1,
    "prompt_identity_transactions.js": 1,
}

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
# `editor_widget.js:12621`, an opaque whole-global-prompt write that carries no
# row identity at all.
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
                is_key = match.group(2) == ":" or (previous and previous[-1] in "{,")
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

# Addressed by durable identity or project-level identity, carrying no lane
# index and no list position, so there is nothing for a rebase to retarget.
#
# Expiry: an entry leaves the day its operation gains a field whose meaning
# depends on the document it was read from -- a list position, a lane index, a
# frame, or a snapshot of a row's current values.
REBASE_EXEMPT = {
    "replace_clip_source": "Names a durable clip_id and an asset_id; no lane or "
                           "position to retarget.",
    "replace_audio_source": "Names a durable track_id and an asset_id.",
    "create_prompt_semantic_unit": "Mints project-level prompt identity; carries "
                                   "no scene geometry.",
    "import_prompt_context_dependencies": "Imports project-level profile and "
                                          "semantic-unit closures; no scene "
                                          "member is addressed.",
}

# No rationale is defensible from the code today. Saying so is the point: a
# plausible-sounding invention would stop the next reader looking. The first
# three are asymmetric with a sibling that *is* rebased; the link pair is not --
# its only sibling, `delete_link_group`, is never emitted and has no case either.
#
# Expiry: umbrella Phase B adopted these (see mutation-authoring-umbrella.md);
# each entry leaves when that phase either adds a case or records a real reason.
REBASE_UNREVIEWED = {
    "create_guide": "Its siblings move_guide, update_guide and delete_guide all "
                    "rebase through rebaseGuideOperation; the create does not, "
                    "and its payload is an opaque `fields`. Unreviewed.",
    "create_prompt_section": "Its siblings update_, delete_ and "
                             "split_prompt_section all rebase through "
                             "rebasePromptOperation; the create does not. "
                             "Unreviewed.",
    "replace_prompt_sections": "Replaces every prompt section in the scene from "
                               "a client-computed list, with no guard and no "
                               "rebase, while a sibling update_scene_fields in "
                               "the same batch carries `expected`. Unreviewed, "
                               "and the sharpest of these.",
    # These two were first written as exempt, on the reasoning that link refs are
    # durable {type, id} pairs. That was invented from the helper's name and is
    # false. `_mutationItemFromSelection` sets `id` to a guide's *frame_index*
    # and a prompt's *list index*, and `_item_ref_from_selection` resolves both
    # positionally on the server (`_find_prompt_section(scene, int(raw_id))`,
    # `_find_guide(scene, int(raw_id))`) whenever the id is not already a durable
    # one. A queued Undo that reorders prompt sections or moves a guide therefore
    # changes what these refs name. Clip and audio members really are durable;
    # the operation is only as safe as its weakest member type.
    "create_link_group": "Carries prompt and guide members addressed by list "
                         "position and frame index, which the server resolves "
                         "against whichever document it loads. Whether a queued "
                         "history action can redirect them is unreviewed.",
    "unlink_items": "Carries the same positionally-resolved refs as "
                    "create_link_group.",
}

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
    "remove_lane": "Carries a lane index with no lane identity guard. "
                   "durable_rules.md already states this in prose -- item lane "
                   "indices and remove_lane carry no lane identity guard -- so "
                   "it is acknowledged, not undiscovered.",
    "split_clip": "The Critical defect itself. split-optimistic-local-apply.md "
                  "L1 adds the `expected` bounds guard; until it lands, this "
                  "entry is the record that the gap is known and owned.",
    "split_audio_track": "As split_clip; the same landing covers it.",
    "update_scene_fields": "Scene-level fields have no row to identify -- the "
                           "scene comes from the URL -- so what an `expected` "
                           "would protect is the field's prior value. Three "
                           "sibling emissions do carry one and these do not; "
                           "why is unreviewed.",
    "unlink_items": "Carries prompt and guide refs the server resolves by list "
                    "position and frame index (`_item_ref_from_selection`), so "
                    "the refs themselves can go stale. Unreviewed.",
    # The two sites differ materially, so the reason names both rather than
    # generalising from the worse one.
    "bulk_delete_items": "Two shapes. `_deleteItemsInLaneWithinGesture` carries "
                         "durable clip/audio ids and a boolean -- there is no "
                         "prior row state an `expected` would describe. "
                         "`_deleteSelectedItemsWithinGesture` passes `items` as "
                         "an identifier, and the array it names DOES carry "
                         "per-item `expected` that the server validates; the "
                         "scan cannot see through the identifier. The real "
                         "residual is narrower than 'unguarded': in the "
                         "`apply_linked` branch non-reference members route "
                         "through `_item_ref_from_selection`, which never reads "
                         "`expected` and resolves prompt and guide positionally.",
    "consolidate_items": "Carries `target_lane` and durable `item_ids`. The lane "
                         "index is not unguarded in the sense that matters -- "
                         "`_rebaseSceneMutationIntentForHistory` retargets it "
                         "through `rebaseLaneIndex`, which the rebase policy "
                         "above records as `rebased`. What carries no guard is "
                         "the item id set, and those are durable.",
    "replace_prompt_sections": "Replaces every prompt section from a "
                               "client-computed list with no guard. The "
                               "sharpest of these; umbrella Phase B owns it.",
    "create_clip": "Additive: there is no prior row for an `expected` to "
                   "describe. Its `track_index` is the rebase question, not this "
                   "one, and `case \"create_clip\"` retargets it.",
    "create_audio_track": "As create_clip; its lane_index is likewise rebased.",
    "create_guide": "Additive, and its fields are opaque to the scan. Its rebase "
                    "policy is separately recorded as unreviewed.",
    "create_prompt_section": "Additive; carries client-computed start/end frames "
                             "with no prior row to compare them against. Its "
                             "rebase policy is separately unreviewed.",
    "create_prompt_semantic_unit": "Mints project-level prompt identity. No "
                                   "scene row exists yet or is named.",
    # The two creates that are NOT purely additive. A name-based exemption would
    # have hidden both, which is why there is no computed create class.
    "create_reference_item": "NOT purely additive, despite the name. Its fields "
                             "carry `end_frame` and `nextStart` read out of the "
                             "client's own `scene.reference_items` -- a prior "
                             "row, in a copy that may be stale -- while a "
                             "sibling update_lane_config in the same batch does "
                             "carry `expected`. `lane_index` is rebased; "
                             "`end_frame` is neither rebased nor guarded.",
    "create_link_group": "NOT purely additive: its entire payload is references "
                         "to pre-existing rows. The client builds per-item "
                         "`expected` and `_item_ref_from_selection` discards it, "
                         "resolving prompt by list index and guide by frame "
                         "index. Same finding as its rebase entry.",
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
    "editor_reference_panel.js:createItem:create_reference_item": [
        ('fields', 'fields.end_frame', 'fields.lane_index', 'fields.members', 'fields.muted', 'fields.nextStart', 'fields.prompt_override', 'fields.sequence_frames', 'fields.start_frame', 'fields.strength', 'type'),
    ],
    "editor_widget.js:_addClipFrameToGuidesWithinGesture:create_guide": [
        ('fields', 'fields.<opaque>', 'type'),
    ],
    "editor_widget.js:_applyPromptSetupWithinGesture:create_prompt_semantic_unit": [
        ('handle_suggestion', 'type', 'unit', 'unit.<opaque>'),
    ],
    "editor_widget.js:_applyPromptSetupWithinGesture:import_prompt_context_dependencies": [
        ('profiles', 'profiles.<opaque>', 'semantic_units', 'semantic_units.<opaque>', 'type'),
    ],
    "editor_widget.js:_applyPromptSetupWithinGesture:replace_prompt_sections": [
        ('sections', 'sections.<opaque>', 'type'),
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
    "editor_widget.js:_deleteSelectedLanesAndItemsWithinGesture:remove_lane": [
        ('item_policy', 'lane_index', 'lane_type', 'type'),
    ],
    "editor_widget.js:_handleAssetDropWithinGesture:create_audio_track": [
        ('fields', 'fields.asset_id', 'fields.lane_index', 'fields.timeline_start_frame', 'type'),
    ],
    "editor_widget.js:_handleAssetDropWithinGesture:create_clip": [
        ('fields', 'fields.asset_id', 'fields.crop_position', 'fields.dual_drop', 'fields.fit_mode', 'fields.role', 'fields.strength', 'fields.timeline_start_frame', 'fields.track_index', 'type'),
        ('fields', 'fields.asset_id', 'fields.audio_lane_index', 'fields.crop_position', 'fields.dual_drop', 'fields.fit_mode', 'fields.link_video_audio', 'fields.targetAudioLane', 'fields.timeline_start_frame', 'fields.track_index', 'type'),
    ],
    "editor_widget.js:_handleAssetDropWithinGesture:create_guide": [
        ('fields', 'fields.<opaque>', 'type'),
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
    "editor_widget.js:_placeReferencePayloadWithinGesture:create_reference_item": [
        ('fields', 'fields.end_frame', 'fields.lane_index', 'fields.members', 'fields.muted', 'fields.nextStart', 'fields.prompt_override', 'fields.sequence_frames', 'fields.start_frame', 'fields.strength', 'type'),
    ],
    "editor_widget.js:_removeLaneDeletingItemsWithinGesture:remove_lane": [
        ('item_policy', 'lane_index', 'lane_type', 'type'),
    ],
    "editor_widget.js:_removeLaneWithItemsWithinGesture:remove_lane": [
        ('item_policy', 'lane_index', 'lane_type', 'target_lane', 'type'),
    ],
    "editor_widget.js:_removeLaneWithinGesture:remove_lane": [
        ('item_policy', 'lane_index', 'lane_type', 'type'),
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
    "editor_widget.js:_splitClipAtFrameWithinGesture:split_audio_track": [
        ('apply_linked', 'frame', 'track_id', 'type'),
    ],
    "editor_widget.js:_splitClipAtFrameWithinGesture:split_clip": [
        ('apply_linked', 'clip_id', 'frame', 'type'),
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
