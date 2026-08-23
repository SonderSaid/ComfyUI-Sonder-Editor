import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server import prompt_context, prompt_tokens


ROOT = Path(__file__).resolve().parents[1]


def _section(attachment, text="body"):
    return {
        "prompt_id": "section",
        "start_frame": 0,
        "end_frame": 24,
        "channels": {"visual": text, "summary": text,
                     "subject_definitions": text},
        "attachments": [attachment],
    }


def _reference_context(subject_ordinal=1, picture_ordinal=1):
    return {
        "setup_manifest": {
            "setup": {"mode": "reference"},
            "pictures": [{"member_id": "portrait", "picture_ordinal": picture_ordinal}],
            "presentation": [{"kind": "picture", "member_id": "portrait",
                              "picture_ordinal": picture_ordinal}],
        },
        "ordinal_manifest": {
            "subjects": {"unit:woman": subject_ordinal},
            "pictures": {"portrait": picture_ordinal},
            "videos": {"motion": 2},
            "audios": {"voice": 3},
        },
        "unit_source_labels": {"unit:woman": [f"<Picture {picture_ordinal}>"]},
        "semantic_units": [{
            "semantic_unit_id": "unit:woman", "name": "Woman",
            "definition": "the person beside @picture(portrait)",
            "sources": [{"entity_id": "woman", "member_id": "portrait"}],
        }],
    }


def _reference_attachment(capability, config):
    return prompt_context.normalize_attachment({
        "kind": "reference",
        "source": {"semantic_unit_ids": ["unit:woman"]},
        "config": config,
        "capabilities": [{
            "capability_id": capability,
            "kind": capability,
            "channel_key": ({"summary": "summary",
                             "definitions": "subject_definitions"}.get(
                                 capability, "visual")),
            "placement": "section_prefix",
        }],
    })


HANDLE_CASES = [
    # (prose, expected handles)
    ("@KWoman is here", ["KWoman"]),
    # An `@` after a word character is an ADDRESS, never a mention.
    ("write to bob@example today", []),
    # ...but prose reaches `@` after punctuation far more often than after a
    # bare space, and an allowlist of openers refused most real sentences.
    ('she said "@KWoman" and left', ["KWoman"]),
    ("(@KWoman) turns", ["KWoman"]),
    ("...@KWoman turns", ["KWoman"]),
    # The run ends at the apostrophe, so possessives survive intact.
    ("@KWoman's jacket", ["KWoman"]),
    # A sentence-ending full stop is punctuation, not a qualifier, and a
    # qualifier is not supported in prose AT ALL: a handle in prose is a
    # mention. `KWoman` matches and `.retention` stays literal prose.
    ("I saw @KWoman.", ["KWoman"]),
    ("@KWoman.retention", ["KWoman"]),
    # The token grammar wins over the handle grammar, because a token carries a
    # stable id and a handle does not. A project may hold a handle named
    # `subject`; without this guard the scanner would eat the kind and strand
    # `(id)` as prose.
    ("@subject(abc-123) walks", []),
    ("@shot(s1) then @KWoman", ["KWoman"]),
    # An email-shaped run with no matching handle still SCANS as one; passing
    # it through unresolved is the resolver's job, not the scanner's.
    ("mail @mail.com now", ["mail"]),
    # Digits and underscores inside, never leading.
    ("@K_Woman2 and @2Woman", ["K_Woman2"]),
    ("", []),
    ("no handles here", []),
    ("@@KWoman", ["KWoman"]),
    ("@KWoman @Beggar", ["KWoman", "Beggar"]),
]


def test_python_and_javascript_handle_grammars_match():
    """The handle scanner is a mirrored pair and must not drift.

    Prose is resolved by Python at compile time and decorated by JavaScript in
    the editor. If the two disagree, the editor paints a handle the compiler
    ignores, or leaves plain a run the compiler rewrites — and the second is
    silent prose corruption, since authored text would change meaning with no
    visible cause.

    Both guards are asserted through real cases rather than by comparing the
    two regex sources, because equal sources with different flags or engines
    would still pass a source comparison.
    """
    expected = {}
    for text, handles in HANDLE_CASES:
        assert [row["handle"] for row in prompt_tokens.handle_mentions(text)] \
            == handles, text
        expected[text] = handles
        # The cheap predicate must agree with the full scan; the compile-entry
        # gate uses it over every channel of every section.
        assert prompt_tokens.has_handle_mention(text) == bool(handles), text

    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for handle grammar parity")
    script = (
        f"const mod = await import("
        f"{json.dumps((ROOT / 'web/js/prompt_tokens.js').as_uri())});\n"
        f"const cases = {json.dumps([text for text, _ in HANDLE_CASES])};\n"
        "console.log(JSON.stringify(Object.fromEntries(cases.map((text) =>"
        " [text, mod.promptHandleMentions(text).map((row) => row.handle)]))));\n")
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == expected


def test_a_handle_mention_reports_the_span_it_occupies():
    """Spans, not spellings: the resolver substitutes in place.

    Returning only the handle would make the caller re-find it, and a second
    search is a second grammar — the exact drift this pair exists to prevent.
    """
    text = "a @KWoman walks past @Beggar."
    rows = prompt_tokens.handle_mentions(text)
    assert [text[row["start"]:row["end"]] for row in rows] == ["@KWoman", "@Beggar"]
    # Source order, so a caller replacing from the end backwards keeps every
    # earlier span valid.
    assert rows[0]["start"] < rows[1]["start"]


def test_a_handle_that_cannot_be_created_cannot_be_written():
    """The scanner and the creation validator agree on what a handle is.

    `PROMPT_HANDLE_RE` gates handle creation. A scanner that accepted MORE than
    it would resolve prose nobody can ever bind; one that accepted less would
    leave a legally created handle unusable in prose.
    """
    for candidate in ["KWoman", "K_Woman2", "a", "A" + "b" * 63]:
        assert prompt_context.PROMPT_HANDLE_RE.fullmatch(candidate), candidate
        assert [row["handle"] for row in
                prompt_tokens.handle_mentions(f"x @{candidate} y")] == [candidate]
    for candidate in ["2Woman", "_Woman", "A" + "b" * 64]:
        assert not prompt_context.PROMPT_HANDLE_RE.fullmatch(candidate), candidate
        assert [row["handle"] for row in prompt_tokens.handle_mentions(
            f"x @{candidate} y")] != [candidate], candidate


def test_python_and_javascript_token_vocabularies_match():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt token grammar parity")
    module_url = (ROOT / "web" / "js" / "prompt_tokens.js").as_uri()
    script = (
        f"const mod = await import({json.dumps(module_url)});\n"
        "console.log(JSON.stringify(Object.fromEntries(Object.entries("
        "mod.PROMPT_TOKEN_KINDS).map(([key,value]) => [key, "
        "{manifest_key:value.manifestKey,label_template:value.labelTemplate,"
        "physical:value.physical}]))));\n"
    )
    js_vocabulary = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    py_vocabulary = prompt_tokens._declarations()
    assert js_vocabulary == py_vocabulary


def test_format_declared_token_kind_has_python_javascript_parity():
    declarations = {
        "persona": {"manifest_key": "people", "label_template": "[Person {n}]",
                    "physical": False},
        "plate": {"manifest_key": "plates", "label_template": "[Plate {n}]",
                  "physical": True},
    }
    py_result = prompt_tokens.resolve(
        "@persona(hero) with @plate(bg)",
        {"people": {"hero": 2}, "plates": {"bg": 4}},
        declarations=declarations,
    )
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt token grammar parity")
    module_url = (ROOT / "web" / "js" / "prompt_tokens.js").as_uri()
    js_declarations = {
        key: {"manifestKey": value["manifest_key"],
              "labelTemplate": value["label_template"],
              "physical": value["physical"]}
        for key, value in declarations.items()
    }
    script = (
        f"const mod = await import({json.dumps(module_url)});\n"
        f"const declarations = {json.dumps(js_declarations)};\n"
        "const value = mod.resolvePromptTokens('@persona(hero) with @plate(bg)', "
        "{people:{hero:2},plates:{bg:4}}, {}, declarations);\n"
        "console.log(JSON.stringify([value.resolvedText,value.unresolvedIds]));\n"
    )
    js_result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert js_result == [py_result[0], py_result[1]]


def test_javascript_builds_token_declarations_from_resolved_profile():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt token grammar parity")
    module_url = (ROOT / "web" / "js" / "prompt_tokens.js").as_uri()
    profile = prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"]
    script = (
        f"const mod = await import({json.dumps(module_url)});\n"
        f"const profile = {json.dumps(profile)};\n"
        "console.log(JSON.stringify(mod.promptTokenDeclarationsFromProfile(profile)));\n"
    )
    declarations = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    expected = {
        key: {
            "manifestKey": value["manifest_key"],
            "labelTemplate": value["label_template"],
            "physical": value["physical"],
        }
        for key, value in prompt_context.prompt_token_declarations(profile).items()
    }
    assert declarations == expected


def test_token_resolves_in_summary_definition_and_custom_text_paths():
    context = _reference_context()
    summary = _reference_attachment(
        "summary", {"summary": "The target follows @subject(unit:woman)"})
    summary_result = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(summary)], window_start=0,
        window_end=24, fps=24, template="minimax_h3_ref", context=context)
    assert "The target follows <Subject 1>" in summary_result["prompt"]

    definition = _reference_attachment("definitions", {})
    definition_result = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(definition)], window_start=0,
        window_end=24, fps=24, template="minimax_h3_ref", context=context)
    assert "<Subject 1> is the person beside <Picture 1>" in definition_result["prompt"]

    custom = prompt_context.normalize_attachment({
        "kind": "custom", "config": {"text": "Track @video(motion) and @audio(voice)"},
    })
    custom_result = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(custom, "")], window_start=0,
        window_end=24, fps=24, template="minimax_h3_ref", context=context)
    assert custom_result["prompt"] == (
        "subject_definitions:\nTrack <Video 2> and <Audio 3>")


def test_reordering_setup_renumbers_without_changing_authored_prose():
    attachment = _reference_attachment(
        "summary", {"summary": "Follow @subject(unit:woman)"})
    first = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(attachment)], window_start=0,
        window_end=24, fps=24, template="minimax_h3_ref",
        context=_reference_context(subject_ordinal=1))
    second = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(attachment)], window_start=0,
        window_end=24, fps=24, template="minimax_h3_ref",
        context=_reference_context(subject_ordinal=2))
    assert "<Subject 1>" in first["prompt"]
    assert "<Subject 2>" in second["prompt"]
    assert attachment["config"]["overrides"]["summary"] == (
        "Follow @subject(unit:woman)")


def test_deleted_token_source_blocks_and_literal_provider_label_is_untouched():
    attachment = _reference_attachment(
        "summary", {"summary": "Follow @subject(unit:deleted), not <Subject 1>"})
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(attachment)], window_start=0,
        window_end=24, fps=24, template="minimax_h3_ref",
        context=_reference_context())
    diagnostic = next(value for value in compiled["errors"]
                      if value["code"] == "unresolved_prompt_token")
    assert diagnostic["attachment_id"] == attachment["attachment_id"]
    assert diagnostic["prompt_token_id"] == "unit:deleted"
    assert "@subject(unit:deleted), not <Subject 1>" in compiled["prompt"]


def _shot(attachment_id):
    return prompt_context.normalize_attachment({
        "attachment_id": attachment_id, "kind": "shot", "config": {},
    })


def _shot_section(prompt_id, start, end, attachments, text="body"):
    return {
        "prompt_id": prompt_id, "start_frame": start, "end_frame": end,
        "channels": {"visual": text, "summary": text,
                     "subject_definitions": text},
        "attachments": attachments,
    }


def test_shot_token_resolves_against_the_marker_counter():
    """PR-20. `[Shot N]` is compiler-owned numbering with no authored spelling,
    so a definition naming its shot had to hard-code a number that went stale as
    soon as a Shot was added earlier. The citation must track the same counter
    that renders the marker."""
    summary = _reference_attachment(
        "summary", {"summary": "The frame opens @shot(shot-b)"})
    compiled = prompt_context.compile_prompt_context(
        global_channels={},
        sections=[
            _shot_section("first", 0, 12, [_shot("shot-a")]),
            _shot_section("second", 12, 24, [_shot("shot-b"), summary]),
        ],
        window_start=0, window_end=24, fps=24, template="minimax_h3_ref",
        context=_reference_context())
    assert not [value for value in compiled["errors"]
                if value["code"] == "unresolved_prompt_token"]
    assert "The frame opens [Shot 2]" in compiled["prompt"]
    # The authored text keeps the stable id — an ordinal must never persist.
    assert summary["config"]["overrides"]["summary"] == "The frame opens @shot(shot-b)"


def test_shot_token_renumbers_when_an_earlier_shot_appears():
    """The staleness PR-20 describes: the same authored citation must compile to
    a different ordinal once a shot opens ahead of it, with no edit to the prose."""
    summary = _reference_attachment(
        "summary", {"summary": "Matches @shot(shot-b)"})
    later = _shot_section("second", 12, 24, [_shot("shot-b"), summary])
    without_earlier = prompt_context.compile_prompt_context(
        global_channels={}, sections=[later], window_start=0, window_end=24,
        fps=24, template="minimax_h3_ref", context=_reference_context())
    with_earlier = prompt_context.compile_prompt_context(
        global_channels={},
        sections=[_shot_section("first", 0, 12, [_shot("shot-a")]), later],
        window_start=0, window_end=24, fps=24, template="minimax_h3_ref",
        context=_reference_context())
    assert "Matches [Shot 1]" in without_earlier["prompt"]
    assert "Matches [Shot 2]" in with_earlier["prompt"]


def test_shot_token_for_a_shot_outside_the_window_blocks():
    """A shot with no ordinal in the selected window is a dangling binding, not
    a silent zero — the same contract every other prompt token follows."""
    summary = _reference_attachment(
        "summary", {"summary": "Matches @shot(shot-missing)"})
    compiled = prompt_context.compile_prompt_context(
        global_channels={},
        sections=[_shot_section("only", 0, 24, [_shot("shot-a"), summary])],
        window_start=0, window_end=24, fps=24, template="minimax_h3_ref",
        context=_reference_context())
    diagnostic = next(value for value in compiled["errors"]
                      if value["code"] == "unresolved_prompt_token")
    assert diagnostic["prompt_token_id"] == "shot-missing"
    assert "@shot(shot-missing)" in compiled["prompt"]


def test_attachment_limit_is_measured_after_token_resolution():
    attachment = prompt_context.normalize_attachment({
        "kind": "custom",
        "config": {"text": " ".join(["@subject(x)"] * 1200)},
    })
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(attachment, "")], window_start=0,
        window_end=24, fps=24, template="minimax_h3_ref",
        context={"setup_manifest": {"setup": {"mode": "reference"}},
                 "ordinal_manifest": {"subjects": {"x": 123456}}})
    assert any(value["code"] == "attachment_output_limit"
               for value in compiled["errors"])


def test_one_attachment_exposes_split_channel_projections_outside_content_hash():
    attachment = prompt_context.normalize_attachment({
        "kind": "reference",
        "source": {"semantic_unit_ids": ["unit:woman"]},
        "config": {"summary": "Summary line", "text": "Mention line"},
        "capabilities": [
            {"capability_id": "definitions", "kind": "definitions",
             "channel_key": "subject_definitions", "placement": "section_prefix"},
            {"capability_id": "summary", "kind": "summary",
             "channel_key": "summary", "placement": "section_prefix"},
            {"capability_id": "mentions", "kind": "mentions",
             "channel_key": "detailed_description", "placement": "section_prefix"},
        ],
    })
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(attachment)], window_start=0,
        window_end=24, fps=24, template="minimax_h3_ref",
        context=_reference_context())

    split = compiled["attachment_channel_previews"][attachment["attachment_id"]]
    assert set(split) == {"subject_definitions", "summary", "detailed_description"}
    assert "<Subject 1>" in split["subject_definitions"]
    assert split["summary"] == "Summary line"
    assert split["detailed_description"] == "Mention line"
    # The field is explanatory projection metadata, not frozen prompt content.
    expected_hash = prompt_context.content_hash({
        "prompt": compiled["prompt"], "channels": compiled["channels"],
        "segments": compiled["segments"], "window": compiled["window"],
        "profile_hash": compiled["profile_hash"],
        "setup_manifest": compiled["setup_manifest"],
    })
    assert compiled["content_hash"] == expected_hash


def test_token_in_authored_prose_stays_literal_and_is_advisory():
    authored = "Keep @subject(unit:woman) literal"
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[{
            "prompt_id": "section", "start_frame": 0, "end_frame": 24,
            "channel_docs": {"visual": {"nodes": [{
                "type": "text", "node_id": "text", "text": authored,
            }]}}, "attachments": [],
        }], window_start=0, window_end=24, fps=24, template="standard",
        context=_reference_context())
    assert authored in compiled["prompt"]
    assert not compiled["errors"]
    advisory = next(value for value in compiled["warnings"]
                    if value["code"] == "authored_prompt_token_literal")
    assert advisory["origin"] == "section"
    assert advisory["channel_key"] == "visual"


def test_declared_token_grammar_matches_between_python_and_javascript():
    """The two sides must agree on kind CASE and on what makes a declaration usable.

    They did not: the browser lowercased `token_kind` and skipped a
    declaration missing its manifest key or label template; Python did
    neither. A format declaring `"Picture"` registered as `picture` in one and
    `Picture` in the other, and one missing its label template registered a
    token on the server that could never render. Breaking either half of the
    guard must fail here.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt token grammar parity")
    profile = {
        "physical_populations": [
            # Mixed case, and a complete declaration.
            {"key": "pictures", "token_kind": "Picture",
             "ordinal_key": "pictures", "label_template": "<Picture {n}>"},
            # Missing its label template: unusable, must be skipped by BOTH.
            {"key": "videos", "token_kind": "video", "ordinal_key": "videos"},
            # Missing its manifest key: likewise.
            {"key": "audios", "token_kind": "audio",
             "label_template": "<Audio {n}>"},
        ],
        "identity_kinds": [],
        "capabilities": {},
    }
    module_url = (ROOT / "web" / "js" / "prompt_tokens.js").as_uri()
    script = (
        f"const mod = await import({json.dumps(module_url)});" + chr(10)
        + f"const profile = {json.dumps(profile)};" + chr(10)
        + "console.log(JSON.stringify(Object.keys("
        "mod.promptTokenDeclarationsFromProfile(profile)).sort()));" + chr(10)
    )
    js_kinds = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    py_kinds = sorted(prompt_context.prompt_token_declarations(profile))
    assert js_kinds == py_kinds
    # And the agreed answer is the lowercased, complete one only.
    assert py_kinds == ["picture"]
