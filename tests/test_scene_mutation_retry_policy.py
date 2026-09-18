"""A scene mutation's retry policy is derived from the addressing it carries.

`durable_rules.md`: *"Derive the policy — never let it be passed in beside the
addressing it is supposed to follow, or a caller acquires retry by omission."*
`_runSceneMutation` defaulted `retryOnConflict` to `true`, which is that
omission exactly: every gesture got the strong policy without stating any
evidence for it.

What these tests protect, in the order the failures matter:

1. **Coverage.** Every dispatcher operation type is classified, and the table
   holds nothing the dispatcher does not. An unclassified type gets no retry at
   runtime, so the tripwire is about *noticing*, not about safety.
2. **The classification is about the SERVER.** Umbrella Phase B's central
   finding is that 23 of 38 dispatch branches ignore any `expected` a client
   sends. A positional operation is promoted to retryable only by a snapshot the
   server compares, so every `promotedBy` is checked against `GUARD_CONTRACTS` —
   the traced record of what each branch validates — and against `routes.py`
   itself. Promoting on a guard the server discards would rebuild the same hole
   the guard tripwire closed, wearing a third hat.
3. **No caller can hand the policy in.** `retryOnConflict: true` at a scene-path
   call site fails the suite; only `false`, a downgrade, is honoured.

These import `test_scene_mutation_registration` rather than re-scanning, as
`mutation-surface-correctness.md` §4 requires: its predicate was wrong twice
before it was right, and a second copy would be a third chance.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import test_scene_mutation_registration as registration
from test_scene_mutation_registration import (
    GUARD_CONTRACTS,
    _code_mask,
    _dispatcher_op_types,
    _match_delimiter,
    _routes_identifiers,
)

ROOT = Path(__file__).resolve().parents[1]
ADDRESSING_JS = ROOT / "web/js/scene_mutation_addressing.js"
WIDGET_JS = ROOT / "web/js/editor_widget.js"

_NOTHING = registration._NOTHING


# ---------------------------------------------------------------------------
# Reading the shipped table out of its own source.
#
# Deliberately NOT by running node: the coverage ratchet must fail on a machine
# with no node, and a skipped ratchet is the same as no ratchet. The behaviour
# tests below DO run node, because behaviour is what the browser executes.
# ---------------------------------------------------------------------------

_ENTRY_RE = re.compile(r"\n    (?P<name>[a-z_]+): freeze\(\{")
_GUARD_RE = re.compile(
    r"guard\(\s*\"(?P<bag>\w+)\"\s*,\s*\{(?P<body>.*?)\}\s*\)", re.S)
_LIST_RE = re.compile(r"(?P<key>identifying|required|ids):\s*(?:freeze\()?\[(?P<body>[^\]]*)\]")
_VALIDATOR_RE = re.compile(r"validator:\s*\"(?P<name>\w+)\"")
_ADDRESSING_RE = re.compile(r"addressing:\s*(?P<value>[A-Z_]+)")
_CITATION_RE = re.compile(r"`([A-Za-z_][\w.]*)`")


def _string_list(body: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\"([^\"]+)\"", body))


def _table_source() -> str:
    source = ADDRESSING_JS.read_text(encoding="utf-8")
    mask = _code_mask(source)
    anchor = source.index("export const SCENE_MUTATION_ADDRESSING")
    brace = source.index("{", source.index("freeze(", anchor))
    end = _match_delimiter(source, brace, "{", "}", mask)
    assert end > 0, "the addressing table literal is unterminated"
    return source[brace:end + 1]


def _entries() -> dict[str, dict]:
    """`{op_type: {addressing, ids, guards, refine, evidence}}` from source."""
    table = _table_source()
    starts = [(match.group("name"), match.start())
              for match in _ENTRY_RE.finditer(table)]
    mask = _code_mask(table)
    entries = {}
    for name, start in starts:
        brace = table.index("{", table.index("freeze(", start))
        end = _match_delimiter(table, brace, "{", "}", mask)
        assert end > 0, f"entry {name} is unterminated"
        body = table[brace:end + 1]
        addressing = _ADDRESSING_RE.search(body)
        ids = ()
        for match in _LIST_RE.finditer(body):
            if match.group("key") == "ids":
                ids = _string_list(match.group("body"))
        guards = []
        for match in _GUARD_RE.finditer(body):
            inner = match.group("body")
            lists = {one.group("key"): _string_list(one.group("body"))
                     for one in _LIST_RE.finditer(inner)}
            validator = _VALIDATOR_RE.search(inner)
            guards.append({
                "bag": match.group("bag"),
                "identifying": lists.get("identifying", ()),
                "required": lists.get("required", ()),
                "validator": validator.group("name") if validator else "",
            })
        entries[name] = {
            "addressing": addressing.group("value") if addressing else "",
            "ids": ids,
            "guards": tuple(guards),
            "refine": "refine(operation)" in body,
            "evidence": body,
        }
    return entries


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


def _evidence_for(operations: str) -> str:
    """Run `deriveRetryOnConflict` / `sceneMutationRetryEvidence` in node."""
    url = ADDRESSING_JS.as_uri()
    return _run_node(f"""
        import {{ sceneMutationRetryEvidence, deriveRetryOnConflict }} from {url!r};
        const cases = {operations};
        for (const one of cases) {{
            console.log(JSON.stringify({{
                ...sceneMutationRetryEvidence(one),
                batch: deriveRetryOnConflict([one]),
            }}));
        }}
    """)


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

def test_every_dispatcher_operation_is_classified():
    """A type the dispatcher accepts but the table does not name gets no retry.

    Silently. That is the failure this ratchet exists to make loud -- the whole
    umbrella is about defaults that nobody chose.
    """
    classified = set(_entries())
    dispatched = set(_dispatcher_op_types())
    assert not dispatched - classified, (
        "scene_mutation_addressing.js does not classify: "
        f"{sorted(dispatched - classified)}. Add an entry with one traced line "
        "of evidence naming the code that resolves the target -- do not leave it "
        "to the unclassified default, which is invisible.")
    assert not classified - dispatched, (
        "scene_mutation_addressing.js classifies operations the dispatcher no "
        f"longer accepts: {sorted(classified - dispatched)}")


def test_every_entry_declares_an_addressing_and_evidence():
    """And the evidence names code, so `evidence: "TODO"` cannot satisfy it.

    That is not hypothetical tidiness: the cheapest repair for the coverage
    ratchet above is to add an entry, and without this the cheapest *entry* is
    one that asserts nothing. Phase A's audit found four exemption reasons that
    were inventions, and the correction it recorded is the standard here -- "a
    reason is a claim about code and has to be traced like one".
    """
    routes_names = _routes_identifiers()
    helper = re.compile(r"^_[a-z][a-z0-9_]*$")
    for name, entry in _entries().items():
        assert entry["addressing"] in {
            "DURABLE", "POSITIONAL", "COLLECTION", "PER_ITEM", "UNADDRESSED"}, (
            f"{name} declares no addressing class")
        cited = [value for value in _CITATION_RE.findall(entry["evidence"])
                 if helper.match(value) and value in routes_names]
        assert cited, (
            f"{name} cites no server helper. Name the function that resolves the "
            "target or compares the snapshot, so the claim can be re-traced "
            "rather than trusted.")
        if entry["addressing"] == "DURABLE":
            assert entry["ids"], f"{name} is durable but names no id field"


def test_the_evidence_cites_code_that_exists():
    """Phase A's lesson: a reason is a claim about code and has to be traced.

    Sixteen of that landing's evidence citations pointed at the previous
    branch's tail before it stopped citing line numbers at all.
    """
    routes_names = _routes_identifiers()
    # Scoped to names shaped like a private server helper. A backticked field
    # name is prose about a payload, and an allow-list of those would grow
    # without bound and certify nothing; the citations worth checking are the
    # ones that claim "this code does that", and every one of those is a
    # `_lower_snake` function or constant in routes.py.
    helper = re.compile(r"^_[a-z][a-z0-9_]*$")
    client_helpers = {"_mutationItemFromSelection", "_applyLocalCreateGuide"}
    unknown = {}
    for name, entry in _entries().items():
        for cited in _CITATION_RE.findall(entry["evidence"]):
            if not helper.match(cited) and cited not in client_helpers:
                continue
            if cited in routes_names or cited in client_helpers:
                continue
            unknown.setdefault(name, []).append(cited)
    assert not unknown, (
        "evidence names server helpers that routes.py does not define: "
        f"{unknown}")


# ---------------------------------------------------------------------------
# The promotion is about the server, not about the emission
# ---------------------------------------------------------------------------

def test_a_promotion_names_a_guard_the_server_actually_compares():
    """The fake-guard hole, third hat.

    L3a stopped an emission from claiming a guard its branch discards. This stops
    the RETRY POLICY from being bought with one. An operation is promoted out of
    positional addressing only by keys `GUARD_CONTRACTS` records the branch
    comparing -- and `GUARD_CONTRACTS` is itself checked against routes.py by
    `test_the_guard_contracts_still_match_the_code`, so the chain ends at code.
    """
    for name, entry in _entries().items():
        if not entry["guards"]:
            continue
        contract, keys, _evidence = GUARD_CONTRACTS[name]
        assert contract != _NOTHING, (
            f"{name} is promoted to retryable by a snapshot, but GUARD_CONTRACTS "
            "records its branch reading no `expected` on any path. A guard the "
            "server discards cannot make a positional write safe to replay.")
        for guard in entry["guards"]:
            named = set(guard["identifying"]) | set(guard["required"])
            if not keys:
                # _WRITTEN_FIELDS / _WHOLE_RECORD / _PER_ITEM carry no closed key
                # set; their compared keys are the payload's own.
                continue
            assert named <= set(keys), (
                f"{name} promotes on {sorted(named - set(keys))}, which "
                f"`{_evidence.split('`')[1] if '`' in _evidence else name}` does "
                "not compare")


def test_every_named_validator_exists_in_routes():
    routes_names = _routes_identifiers()
    for name, entry in _entries().items():
        for guard in entry["guards"]:
            assert guard["validator"], f"{name} promotes with no named validator"
            assert guard["validator"] in routes_names, (
                f"{name} names `{guard['validator']}`, which routes.py does not "
                "define")


def test_an_operation_with_no_server_side_guard_is_not_promoted():
    """The direction that matters most: `_NOTHING` must stay unpromotable."""
    entries = _entries()
    for name, (contract, _keys, _evidence) in GUARD_CONTRACTS.items():
        if contract != _NOTHING:
            continue
        entry = entries.get(name)
        assert entry is not None, f"{name} is not classified"
        assert not entry["guards"], (
            f"{name} reads no `expected` on any path, so it cannot be promoted "
            "by one")


def test_create_reference_item_is_not_promoted_by_its_neighbour_measurement():
    """A guard that measures something is not a guard that identifies the row.

    `_validate_reference_creation_identity` compares `next_start_frame` -- where
    the next item on the lane starts -- which refuses a moved neighbour and says
    nothing about whether `fields.lane_index` still means the same lane. The
    identical argument was made for `_require_no_reference_overlap` owning one
    direction of that guard, and an audit disproved it with a counterexample.
    """
    entry = _entries()["create_reference_item"]
    assert entry["addressing"] == "POSITIONAL"
    assert not entry["guards"]
    assert GUARD_CONTRACTS["create_reference_item"][1] == frozenset({"next_start_frame"})


# ---------------------------------------------------------------------------
# No caller hands the policy in
# ---------------------------------------------------------------------------

def _run_scene_mutation_source() -> str:
    source = WIDGET_JS.read_text(encoding="utf-8")
    start = source.index("    _runSceneMutation(operations, {")
    end = source.index("\n    _acceptPromptAttachmentConfiguration(", start)
    return source[start:end]


def test_run_scene_mutation_derives_rather_than_defaults():
    body = _run_scene_mutation_source()
    assert "retryOnConflict = null" in body, (
        "`retryOnConflict` must default to null (derive). A `true` default is "
        "the 'acquire retry by omission' hazard durable_rules.md names.")
    assert "deriveRetryOnConflict(queuedIntent.operations)" in body, (
        "the policy must be derived from the operations the queue actually "
        "sends -- a merge can combine several gestures' operations, so deriving "
        "at the call site would describe a payload that is not the body")
    assert "retryOnConflict === false" in body, (
        "only an explicit `false` may lower the derived policy")


# Every client call whose URL literal targets the scene-mutations route. There
# are two, and the second is the one an earlier version of this tripwire could
# not see: `_handleAssetDropWithinGesture` builds its own request through
# `_runVersionedProjectMutation` instead of `_runSceneMutation`, and so inherited
# that method's blind `retryOnConflict = true` default while the derivation went
# past it. Scanning the ROUTE rather than the helper is what makes a third
# poster fail the suite instead of silently opting out.
_SCENE_MUTATIONS_URL_RE = re.compile(r"/scenes/\$\{[^}]*\}/mutations")


def _scene_mutation_posters():
    """`(module, line, call_extent)` for every post to the scene-mutations route."""
    found = []
    for module in ("editor_widget.js", "editor_prompt_panel.js",
                   "editor_reference_panel.js", "prompt_context_chips.js",
                   "prompt_identity_transactions.js"):
        source = (ROOT / "web/js" / module).read_text(encoding="utf-8")
        mask = _code_mask(source)
        for match in _SCENE_MUTATIONS_URL_RE.finditer(source):
            # The URL sits inside a template literal, so the mask reads 0 there;
            # walk out to the enclosing call instead of testing the match itself.
            call = source.rindex("(", 0, match.start())
            while call and not mask[call]:
                call = source.rindex("(", 0, call)
            end = _match_delimiter(source, call, "(", ")", mask)
            if end < 0:
                continue
            found.append((module, source.count(chr(10), 0, call) + 1,
                          source[call:end + 1]))
    return found


def test_every_post_to_the_scene_mutations_route_derives_its_policy():
    posters = _scene_mutation_posters()
    assert len(posters) == 2, (
        f"expected two posters to the scene-mutations route, found {len(posters)}: "
        f"{[(one[0], one[1]) for one in posters]}. A new one must derive its retry "
        "policy from the operations it sends, exactly as the other two do -- "
        "`_runVersionedProjectMutation` defaults to retrying, which is the blind "
        "policy this landing removed.")
    for module, line, extent in posters:
        assert "retryOnConflict" in extent, (
            f"{module}:{line} posts scene mutations without stating a retry "
            "policy, so it inherits `_runVersionedProjectMutation`'s "
            "`retryOnConflict = true`")


def test_the_drop_path_derives_the_same_policy():
    """The drop is the gesture with the most to lose from a blind replay.

    It sends `set_lane_count` plus a `create_clip`/`create_audio_track` whose
    destination is a lane INDEX, and `create_clip{dual_drop}` runs an ffmpeg
    extraction before the commit -- so a replay both lands the media on the wrong
    lane and extracts a second time.
    """
    source = WIDGET_JS.read_text(encoding="utf-8")
    start = source.index("const queueDropMutation = (")
    extent = source[start:start + 3000]
    assert "deriveRetryOnConflict(operations)" in extent, (
        "the drop path must derive its policy like `_runSceneMutation`")
    assert "maxAttempts: retry ? 2 : 1" in extent


def test_no_scene_path_caller_claims_a_retry():
    """`retryOnConflict: true` anywhere a scene mutation is enqueued.

    Only a downgrade is honoured at runtime, so such a call site would be inert
    -- which is precisely why it must fail the suite rather than mislead the next
    reader into thinking the policy is a caller's to set.
    """
    offenders = []
    for module in ("editor_widget.js", "editor_prompt_panel.js",
                   "editor_reference_panel.js", "prompt_context_chips.js",
                   "prompt_identity_transactions.js"):
        source = (ROOT / "web/js" / module).read_text(encoding="utf-8")
        mask = _code_mask(source)
        # Brace-match each call's argument list rather than guessing a window:
        # a proximity heuristic reports whichever call happens to be nearby, and
        # the emitting modules are large enough that "nearby" means nothing.
        for match in re.finditer(r"_runSceneMutation\s*\(", source):
            if not mask[match.start()]:
                continue
            open_paren = source.index("(", match.start())
            end = _match_delimiter(source, open_paren, "(", ")", mask)
            if end < 0:
                continue
            extent = source[open_paren:end + 1]
            extent_mask = mask[open_paren:end + 1]
            for inner in re.finditer(r"retryOnConflict:\s*true", extent):
                if extent_mask[inner.start()]:
                    offenders.append(
                        f"{module}:"
                        f"{source.count(chr(10), 0, open_paren + inner.start()) + 1}")
    assert not offenders, (
        f"scene-path call sites claim a retry at {offenders}; the policy is "
        "derived from the operations, so `true` is inert as well as wrong")


def test_queue_project_mutation_declares_no_retry_option():
    """The sibling method must not carry the shape this landing removed.

    It never used one -- it was destructured and dropped -- so a caller passing
    it would be silently ignored. An unused option that reads like a policy is
    exactly the omission hazard.
    """
    source = WIDGET_JS.read_text(encoding="utf-8")
    start = source.index("    _queueProjectMutation({")
    end = source.index(chr(10) + "    _runSceneMutation(", start)
    body = source[start:end]
    assert "retryOnConflict = " not in body, (
        "`_queueProjectMutation` declares a retry option it does not use")


def test_a_caller_that_declines_a_retry_also_declines_coalescing():
    """Because the override is closure-captured and the intent is not.

    The queue replaces a coalesced entry's `intent` AND its `run` with the
    joining gesture's, so the derivation always describes the body. A caller's
    explicit `false` lives only in its own `run` closure, and a `merge` that
    combined two intents would carry the declining gesture's operations under the
    surviving gesture's policy. Both opt-outs pass `coalesce: false`, which makes
    that unreachable -- this keeps it unreachable rather than incidental.
    """
    source = WIDGET_JS.read_text(encoding="utf-8")
    mask = _code_mask(source)
    for match in re.finditer(r"_runSceneMutation\s*\(", source):
        if not mask[match.start()]:
            continue
        open_paren = source.index("(", match.start())
        end = _match_delimiter(source, open_paren, "(", ")", mask)
        if end < 0:
            continue
        extent = source[open_paren:end + 1]
        if "retryOnConflict: false" not in extent:
            continue
        line = source.count(chr(10), 0, open_paren) + 1
        assert "coalesce: false" in extent, (
            f"editor_widget.js:{line} declines a retry but allows coalescing; a "
            "joining gesture would replace the `run` that holds the refusal")


def test_the_two_deliberate_opt_outs_are_overrides_and_say_so():
    """Both are downgrades of a policy the derivation would grant.

    The plan recorded that the linked-attachment batch 'derives false correctly'.
    It does not: every member carries the full `_PROMPT_KEYS` snapshot including
    `prompt_id`, which `_validate_prompt_identity` compares, so the addressing
    qualifies and the `false` is the caller's own choice. Recorded here because a
    later session reading the plan would otherwise expect the derivation to
    reproduce it.
    """
    source = WIDGET_JS.read_text(encoding="utf-8")
    for anchor in ("_updateLinkedPromptAttachmentWithinGesture",
                   'label: "apply prompt setup"'):
        start = source.index(anchor)
        window = source[start:start + 6000]
        assert "retryOnConflict: false" in window, f"{anchor} lost its override"
        assert "caller override" in window, (
            f"{anchor} opts out with no statement that it is an override of a "
            "policy the derivation grants")


# ---------------------------------------------------------------------------
# Behaviour, in the module the browser loads
# ---------------------------------------------------------------------------

def test_a_durable_id_qualifies_and_a_missing_one_does_not():
    out = _evidence_for("""[
        { type: 'update_clip', clip_id: 'c1', fields: {} },
        { type: 'update_clip', fields: {} },
        { type: 'delete_reference_item', reference_item_id: 'r1' },
        { type: 'create_prompt_semantic_unit', unit: { semantic_unit_id: 'u1' } },
        { type: 'create_prompt_semantic_unit', unit: {} }
    ]""")
    assert [line for line in out.splitlines() if line.strip()].__len__() == 5
    flags = ['"retryable":true' in line for line in out.splitlines() if line.strip()]
    assert flags == [True, False, True, True, False]


def test_a_positional_row_needs_an_identity_the_server_compares():
    out = _evidence_for("""[
        { type: 'update_guide', frame_index: 10, expected: { guide_id: 'g1' }, fields: {} },
        { type: 'update_guide', frame_index: 10, expected: { guide_id: '' }, fields: {} },
        { type: 'update_guide', frame_index: 10, expected: { asset_id: 'a1' }, fields: {} },
        { type: 'update_prompt_section', index: 0, expected: { prompt_id: 'p1' }, fields: {} },
        { type: 'update_prompt_section', index: 0, expected: { attachments: [] }, fields: {} },
        { type: 'swap_prompt_sections', index_a: 0, index_b: 1,
          expected_a: { prompt_id: 'a' }, expected_b: { prompt_id: 'b' } },
        { type: 'swap_prompt_sections', index_a: 0, index_b: 1,
          expected_a: { prompt_id: 'a' }, expected_b: { start_frame: 0 } }
    ]""")
    flags = ['"retryable":true' in line for line in out.splitlines() if line.strip()]
    assert flags == [True, False, False, True, False, True, False], (
        "a blank id, a non-identifying key, or one half of a swap must all "
        "degrade to positional")


def test_update_lane_config_is_decided_per_lane_family():
    """§1c's per-field partiality, at runtime.

    `_apply_lane_config` compares `expected.lane_id` only when the descriptor has
    a `recipe_attr`. A fixed-config lane never reads `lane_index`; a variable
    family without a recipe has its `expected` discarded.
    """
    out = _evidence_for("""[
        { type: 'update_lane_config', lane_type: 'guide', fields: { hidden: true } },
        { type: 'update_lane_config', lane_type: 'prompt', fields: { locked: true } },
        { type: 'update_lane_config', lane_type: 'video', lane_index: 2, fields: {},
          expected: { lane_id: 'not-compared' } },
        { type: 'update_lane_config', lane_type: 'reference', lane_index: 2, fields: {},
          expected: { lane_id: 'L1' } },
        { type: 'update_lane_config', lane_type: 'reference', lane_index: 2, fields: {} }
    ]""")
    flags = ['"retryable":true' in line for line in out.splitlines() if line.strip()]
    assert flags == [True, True, False, True, False]


def test_a_lane_count_field_degrades_update_scene_fields():
    """`_set_scene_lane_count` pops to an absolute count from the caller's copy."""
    out = _evidence_for("""[
        { type: 'update_scene_fields', fields: { width: 640, height: 480 } },
        { type: 'update_scene_fields', fields: { duration_frames: 200 } },
        { type: 'update_scene_fields', fields: { video_lane_count: 3 } }
    ]""")
    flags = ['"retryable":true' in line for line in out.splitlines() if line.strip()]
    assert flags == [True, True, False]


def test_per_item_operations_decide_member_by_member():
    out = _evidence_for("""[
        { type: 'bulk_delete_items', items: [{ type: 'clip', id: 'c1' }] },
        { type: 'bulk_delete_items', items: [{ type: 'guide', id: 10 }] },
        { type: 'bulk_delete_items', items: [
            { type: 'guide', id: 10, expected: { guide_id: 'g1' } },
            { type: 'clip', id: 'c1' }] },
        { type: 'bulk_delete_items', items: [
            { type: 'guide', id: 10, expected: { guide_id: 'g1' } },
            { type: 'prompt', index: 0 }] },
        { type: 'unlink_items', items: [] }
    ]""")
    flags = ['"retryable":true' in line for line in out.splitlines() if line.strip()]
    assert flags == [True, False, True, False, False], (
        "one unnamed member must deny the whole request, and an empty items "
        "list is not a positive claim about anything")


def test_a_batch_is_as_weak_as_its_weakest_operation():
    url = ADDRESSING_JS.as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';
        import {{ deriveRetryOnConflict }} from {url!r};
        const strong = {{ type: 'update_clip', clip_id: 'c1', fields: {{}} }};
        const weak = {{ type: 'set_lane_count', lane_type: 'video', count: 3 }};
        assert.equal(deriveRetryOnConflict([strong, strong]), true);
        assert.equal(deriveRetryOnConflict([strong, weak]), false);
        assert.equal(deriveRetryOnConflict([weak, strong]), false);
        assert.equal(deriveRetryOnConflict([]), false);
        assert.equal(deriveRetryOnConflict(null), false);
    """)


def test_an_unclassified_operation_type_gets_no_retry():
    url = ADDRESSING_JS.as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';
        import {{ deriveRetryOnConflict, sceneMutationRetryEvidence }} from {url!r};
        const unknown = {{ type: 'reticulate_splines', id: 'x' }};
        assert.equal(deriveRetryOnConflict([unknown]), false);
        assert.equal(sceneMutationRetryEvidence(unknown).retryable, false);
        assert.equal(sceneMutationRetryEvidence({{}}).retryable, false);
    """)


def test_the_two_opt_out_batches_would_otherwise_qualify():
    """Both overrides lower a policy the addressing grants -- proven, not assumed."""
    out = _evidence_for("""[
        { type: 'create_prompt_semantic_unit', unit: { semantic_unit_id: 'u1' } },
        { type: 'import_prompt_context_dependencies', profiles: [], semantic_units: [] },
        { type: 'replace_prompt_sections', sections: [], expected: { sections: [] } },
        { type: 'update_scene_fields', fields: { duration_frames: 300 } },
        { type: 'update_prompt_section', index: 0,
          expected: { prompt_id: 'p1', start_frame: 0, end_frame: 10, prompt: '',
                      muted: false, channels: {}, channel_docs: {}, attachments: [],
                      global_channel_exceptions: [] },
          fields: { attachments: [] } }
    ]""")
    flags = ['"retryable":true' in line for line in out.splitlines() if line.strip()]
    assert flags == [True, True, True, True, True]


# ---------------------------------------------------------------------------
# The wiring, through the real `_runSceneMutation`
# ---------------------------------------------------------------------------

def _run_widget_node(body: str) -> None:
    """The `_run_gesture_node` shape, narrowed to what `_runSceneMutation` needs."""
    widget_url = (ROOT / "web/js/editor_widget.js").as_uri()
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';
        globalThis.window = {{
            comfyAPI: {{ api: {{ api: {{ apiURL: (path) => path }} }} }},
            localStorage: {{ getItem: () => null }},
        }};
        globalThis.location = {{ href: 'http://test/' }};
        globalThis.requestAnimationFrame = () => 0;
        const {{ EditorWidget }} = await import({widget_url!r});
        const {{ ProjectMutationQueue }} = await import({queue_url!r});
        const sent = [];
        function makeWidget() {{
            const widget = Object.create(EditorWidget.prototype);
            Object.assign(widget, {{
                projectDir: 'project', activeSceneId: 'scene',
                activeScene: {{ scene_id: 'scene' }},
                _activeMutationGesture: null, _timelineMutationDepth: 0,
                _sceneMutationInvalidationSeq: 0,
                _projectMutationQueue: new ProjectMutationQueue(),
                _claimHistoryPostSnapshotCapture: () => null,
                _stampHistoryPostSnapshot: () => {{}},
                _reconcileActiveSceneFromMutation: () => true,
                _schedulePostMutationSceneRefresh: () => {{}},
                _deferProjectBackedRefresh: () => {{}},
                _replayDeferredProjectBackedRefresh: () => {{}},
                _snapshotMutationDiagnostics: () => null,
                _mutationDiagnosticHeaders: () => ({{}}),
                _fetchScenes: async () => {{}},
                _renderSceneAfterLocalMutation: () => {{}},
                _renderTimeline: () => {{}}, _renderViewportFrame: () => {{}},
                _runVersionedProjectMutation: async (path, init, options) => {{
                    sent.push({{ operations: JSON.parse(init.body).operations, options }});
                    return {{ payload: {{ status: 'ok' }} }};
                }},
            }});
            return widget;
        }}
        {body}
    """)


def test_the_derived_policy_reaches_the_request():
    _run_widget_node("""
        const w = makeWidget();
        await w._runSceneMutation(
            [{ type: 'update_clip', clip_id: 'c1', fields: { muted: true } }],
            { key: 'a', refreshScenes: false, coalesce: false });
        await w._runSceneMutation(
            [{ type: 'set_lane_count', lane_type: 'video', count: 3 }],
            { key: 'b', refreshScenes: false, coalesce: false });
        assert.equal(sent.length, 2);
        assert.deepEqual(sent.map(one => one.options.retryOnConflict), [true, false]);
        assert.deepEqual(sent.map(one => one.options.maxAttempts), [2, 1]);
    """)


def test_a_caller_may_lower_the_policy_but_never_raise_it():
    _run_widget_node("""
        const w = makeWidget();
        // A qualifying batch, declined by its caller.
        await w._runSceneMutation(
            [{ type: 'update_clip', clip_id: 'c1', fields: {} }],
            { key: 'a', refreshScenes: false, coalesce: false, retryOnConflict: false });
        // A non-qualifying batch whose caller claims a retry: ignored.
        await w._runSceneMutation(
            [{ type: 'set_lane_count', lane_type: 'video', count: 3 }],
            { key: 'b', refreshScenes: false, coalesce: false, retryOnConflict: true });
        assert.deepEqual(sent.map(one => one.options.retryOnConflict), [false, false]);
    """)


def test_coalescing_derives_from_the_operations_the_queue_actually_sends():
    """The reason the derivation lives inside `run` and not at the call site.

    The queue replaces a coalesced entry's intent with the survivor's, so a
    policy derived at enqueue would describe a payload that is not the body.
    Here the weak gesture arrives second and must take the whole request down
    with it.
    """
    _run_widget_node("""
        const w = makeWidget();
        const first = w._runSceneMutation(
            [{ type: 'update_clip', clip_id: 'c1', fields: { muted: true } }],
            { key: 'same', refreshScenes: false });
        const second = w._runSceneMutation(
            [{ type: 'set_lane_count', lane_type: 'video', count: 3 }],
            { key: 'same', refreshScenes: false });
        await Promise.all([first, second]);
        assert.equal(sent.length, 1, 'the two gestures must coalesce');
        assert.deepEqual(sent[0].operations.map(one => one.type), ['set_lane_count']);
        assert.equal(sent[0].options.retryOnConflict, false);
    """)


# ---------------------------------------------------------------------------
# The ratchet that makes the coverage tripwire's cheapest repair the safe one
# ---------------------------------------------------------------------------

# One best-case payload per dispatcher operation: every durable id present, every
# promotion guard satisfied. "Best case" is the point -- it asks what the table
# grants an operation when the client does everything right, which is the only
# question a lazily-added coverage entry can hide.
CANONICAL_PAYLOAD = {
    "update_scene_fields": {"fields": {"width": 640}},
    "update_lane_configs": {"fields": {}},
    "update_lane_config": {"lane_type": "reference", "lane_index": 0,
                           "fields": {}, "expected": {"lane_id": "L1"}},
    "set_lane_count": {"lane_type": "video", "count": 2},
    "remove_lane": {"lane_type": "video", "lane_index": 0,
                    "expected": {"lane_count": 2, "config": {}}},
    "move_lane": {"lane_type": "reference", "from_index": 0, "to_index": 1,
                  "expected": {"from_lane_id": "a", "to_lane_id": "b"}},
    "consolidate_items": {"lane_type": "video", "target_lane": 0,
                          "item_ids": ["a", "b"]},
    "create_clip": {"fields": {"track_index": 0}},
    "create_audio_track": {"fields": {"lane_index": 0}},
    "create_link_group": {"items": [{"type": "clip", "id": "c1"}]},
    "unlink_items": {"items": [{"type": "clip", "id": "c1"}]},
    "delete_link_group": {"group_id": "g1"},
    "update_clip": {"clip_id": "c1", "fields": {"muted": True}},
    "replace_clip_source": {"clip_id": "c1", "asset_id": "a1"},
    "delete_clip": {"clip_id": "c1"},
    "update_audio_track": {"track_id": "t1", "fields": {"volume": 1}},
    "replace_audio_source": {"track_id": "t1", "asset_id": "a1"},
    "delete_audio_track": {"track_id": "t1"},
    "create_reference_item": {"fields": {"lane_index": 0},
                              "expected": {"next_start_frame": 5}},
    "update_reference_item": {"reference_item_id": "r1", "fields": {"strength": 1},
                              "expected": {"strength": 1}},
    "delete_reference_item": {"reference_item_id": "r1", "expected": {}},
    "split_reference_item": {"reference_item_id": "r1", "frame": 5, "expected": {}},
    "bulk_delete_items": {"items": [{"type": "reference", "id": "r1"}]},
    "split_clip": {"clip_id": "c1", "frame": 5},
    "split_audio_track": {"track_id": "t1", "frame": 5},
    "move_guide": {"from_frame_index": 1, "to_frame_index": 2,
                   "expected": {"guide_id": "g1", "replaces_guide_id": ""}},
    "update_guide": {"frame_index": 1, "fields": {},
                     "expected": {"guide_id": "g1"}},
    "delete_guide": {"frame_index": 1, "expected": {"guide_id": "g1"}},
    "create_guide": {"fields": {"frame_index": 1},
                     "expected": {"replaces_guide_id": ""}},
    "update_prompt_section": {"index": 0, "fields": {},
                              "expected": {"prompt_id": "p1"}},
    "delete_prompt_section": {"index": 0, "expected": {"prompt_id": "p1"}},
    "split_prompt_section": {"index": 0, "frame": 5,
                             "expected": {"prompt_id": "p1"}},
    "create_prompt_section": {"fields": {"start_frame": 0, "end_frame": 5}},
    "replace_prompt_sections": {"sections": [], "expected": {"sections": []}},
    "import_prompt_context_dependencies": {"profiles": [], "semantic_units": []},
    "create_prompt_semantic_unit": {"unit": {"semantic_unit_id": "u1"}},
    "delete_prompt_semantic_unit_if_unreferenced": {"semantic_unit_id": "u1",
                                                    "expected": {}},
    "swap_prompt_sections": {"index_a": 0, "index_b": 1,
                             "expected_a": {"prompt_id": "a"},
                             "expected_b": {"prompt_id": "b"}},
}

# The operations that CANNOT be replayed even when the client does everything
# right. Pinned, so that classifying a new operation as replayable is a visible
# edit rather than the cheapest way to satisfy the coverage tripwire.
NEVER_RETRYABLE = frozenset({
    "update_lane_configs",      # legacy positional whole-array replace
    "set_lane_count",           # absolute count read from the caller's own copy
    "remove_lane",              # guard compares the config AT the index, not uniquely
    "consolidate_items",        # positional target lane, and it removes vacated lanes
    "create_clip",              # fields.track_index
    "create_audio_track",       # fields.lane_index
    "create_reference_item",    # fields.lane_index; the guard measures a neighbour
    "create_prompt_section",    # a destination span, with only an overlap check
})


def test_the_retryable_set_under_a_best_case_payload_is_pinned():
    assert set(CANONICAL_PAYLOAD) == set(_dispatcher_op_types()), (
        "every dispatcher operation needs a best-case payload here; missing "
        f"{sorted(set(_dispatcher_op_types()) - set(CANONICAL_PAYLOAD))}, extra "
        f"{sorted(set(CANONICAL_PAYLOAD) - set(_dispatcher_op_types()))}")
    cases = json.dumps([dict(payload, type=op)
                        for op, payload in sorted(CANONICAL_PAYLOAD.items())])
    rows = [json.loads(line) for line in _evidence_for(cases).splitlines()
            if line.strip()]
    denied = {row["type"] for row in rows if not row["retryable"]}
    assert denied == NEVER_RETRYABLE, (
        "the set of operations that cannot be replayed changed.\n"
        f"  newly replayable: {sorted(NEVER_RETRYABLE - denied)}\n"
        f"  newly refused:    {sorted(denied - NEVER_RETRYABLE)}\n"
        "Both directions cost something -- granting a replay can write the wrong "
        "row, refusing one discards a correct edit -- so update this pin only "
        "with the traced reason in the table's evidence.")


def test_a_lane_destination_field_degrades_a_durable_row():
    """A durable id says WHAT moves, not WHERE it lands.

    `_rebaseSceneMutationIntentForHistory` retargets `fields.track_index` through
    `rebaseLaneIndex` precisely because a pass-through index rewrites the other
    lane; an HTTP replay is a pass-through index against a document the client
    never read.
    """
    out = _evidence_for("""[
        { type: 'update_clip', clip_id: 'c1', fields: { muted: true } },
        { type: 'update_clip', clip_id: 'c1', fields: { track_index: 1,
            timeline_start_frame: 0, timeline_end_frame: 10 } },
        { type: 'update_audio_track', track_id: 't1', fields: { volume: 1 } },
        { type: 'update_audio_track', track_id: 't1', fields: { lane_index: 2 } },
        { type: 'update_reference_item', reference_item_id: 'r1',
          fields: { strength: 1 }, expected: { strength: 1 } },
        { type: 'update_reference_item', reference_item_id: 'r1',
          fields: { lane_index: 0 }, expected: { lane_index: 0 } }
    ]""")
    flags = ['"retryable":true' in line for line in out.splitlines() if line.strip()]
    assert flags == [True, False, True, False, True, False]


def test_a_reference_member_is_the_strongest_member_type_not_an_unknown_one():
    """`_apply_bulk_delete_items` has five member types, and this was the fifth.

    It resolves a durable `reference_item_id` and compares the WHOLE stored
    record, so it satisfies both qualifying clauses at once. Because one weak
    member denies the batch, omitting it stripped retry from every selection that
    happened to contain a Reference item.
    """
    out = _evidence_for("""[
        { type: 'bulk_delete_items', items: [{ type: 'reference', id: 'r1' }] },
        { type: 'bulk_delete_items', items: [
            { type: 'clip', id: 'c1' }, { type: 'reference', id: 'r1' }] },
        { type: 'bulk_delete_items', items: [{ type: 'reference' }] }
    ]""")
    flags = ['"retryable":true' in line for line in out.splitlines() if line.strip()]
    assert flags == [True, True, False]
