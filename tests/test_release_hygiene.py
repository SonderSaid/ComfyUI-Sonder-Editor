from __future__ import annotations

import re
import json
import os
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
COMFY_OWNED_PACKAGES = {"torch", "torchaudio"}


def _documentation_zip_mentions(root: Path) -> list[str]:
    """Local editorial guard; not a model of GitHub's undisclosed detector."""
    findings = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in {
            ".git", ".claude", ".codex", ".agents", "node_modules",
            "__pycache__", ".pytest_cache", ".venv", "venv",
        } and not (Path(directory) / d).is_symlink()]
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root)
            markdown = path.suffix.lower() in {".md", ".markdown", ".mdx"}
            workflow = relative.parts[0] == "example_workflows" and path.suffix.lower() == ".json"
            if not (markdown or workflow):
                continue
            text = path.read_text(encoding="utf-8")
            if workflow:
                # Decode JSON escapes and inspect both positional and named note copies.
                def strings(value):
                    if isinstance(value, str):
                        yield value
                    elif isinstance(value, dict):
                        for child in value.values():
                            yield from strings(child)
                    elif isinstance(value, list):
                        for child in value:
                            yield from strings(child)
                text = "\n".join(strings(json.loads(text)))
            for number, line in enumerate(text.splitlines(), 1):
                if re.search(r"\bzip\b", unquote(line), re.IGNORECASE):
                    label = "decoded text line" if workflow else "line"
                    findings.append(f"{relative}:{label} {number}: {line[:180]}")
    return findings


def test_public_documentation_has_no_zip_mentions():
    # Temporary project publishing constraint following support review. Keep
    # private recovery records outside the repository. Remote release text and
    # repository metadata require a separate review; this test cannot see them.
    findings = _documentation_zip_mentions(ROOT)
    assert not findings, "ZIP mention in public documentation; review distribution guidance:\n" + "\n".join(findings)


def test_documentation_zip_guard_covers_markdown_and_encoded_workflow_notes(tmp_path):
    (tmp_path / "README.md").write_text("Get Project-Sample.ZIP", encoding="utf-8")
    (tmp_path / "guide.mdx").write_text("Download a zip", encoding="utf-8")
    (tmp_path / "guide.markdown").write_text("https://example.org/sample%2Ezip", encoding="utf-8")
    workflows = tmp_path / "example_workflows"
    workflows.mkdir()
    (workflows / "demo.json").write_text(
        '{"nodes":[{"widgets_values":["sample.\\u007aip"],'
        '"widgets_values_named":{"text":"Download ZIP"}}]}', encoding="utf-8")
    (tmp_path / "implementation.py").write_text("zip(a, b)", encoding="utf-8")
    assert len(_documentation_zip_mentions(tmp_path)) == 5
    for path in [tmp_path / "README.md", tmp_path / "guide.mdx", tmp_path / "guide.markdown"]:
        path.write_text("Install with ComfyUI Manager or git clone.", encoding="utf-8")
    (workflows / "demo.json").write_text('{"nodes":[]}', encoding="utf-8")
    assert _documentation_zip_mentions(tmp_path) == []


def _normalized_requirement_name(requirement: str) -> str:
    requirement = requirement.split(";", 1)[0].strip()
    name = re.split(r"\s*(?:\[|==|~=|!=|<=|>=|<|>|=)", requirement, maxsplit=1)[0]
    return name.strip().lower().replace("_", "-")


def _requirements_txt_names() -> set[str]:
    names = set()
    for raw_line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        names.add(_normalized_requirement_name(line))
    return names


def _pyproject_dependency_names() -> set[str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"(?ms)^dependencies\s*=\s*\[(.*?)^\]", text)
    assert match, "pyproject.toml must declare [project].dependencies"
    return {_normalized_requirement_name(dep) for dep in re.findall(r'"([^"]+)"', match.group(1))}


def test_install_dependency_metadata_stays_in_sync():
    assert _pyproject_dependency_names() == _requirements_txt_names()


def test_install_metadata_does_not_install_comfy_owned_torch_packages():
    declared = _pyproject_dependency_names() | _requirements_txt_names()
    assert declared.isdisjoint(COMFY_OWNED_PACKAGES)


def test_gitignore_excludes_local_release_noise_and_secret_files():
    ignored = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    required = {
        ".pytest_cache/",
        ".mypy_cache/",
        ".ruff_cache/",
        ".coverage",
        ".env",
        ".env.*",
        "!.env.example",
        ".claude/",
        ".agents/",
        ".codex/",
        "dist/",
        "build/",
    }
    assert required <= ignored


# cp1252 with its five undefined slots passed through, which is how a tool that
# wrote UTF-8 bytes through a Windows ANSI codec mangles a non-ASCII literal.
_CP1252_TO_BYTE = {chr(code): code for code in range(0x20, 0x80)}
for _byte in range(0x80, 0x100):
    try:
        _CP1252_TO_BYTE[bytes([_byte]).decode("cp1252")] = _byte
    except UnicodeDecodeError:
        pass
_CP1252_TO_BYTE.update({chr(code): code for code in (0x81, 0x8D, 0x8F, 0x90, 0x9D)})
_LEAD = "".join(map(chr, range(0xC2, 0xF5)))
_CONT = "".join(char for char, byte in _CP1252_TO_BYTE.items() if 0x80 <= byte <= 0xBF)
_MOJIBAKE = re.compile(f"[{re.escape(_LEAD)}][{re.escape(_CONT)}]{{1,3}}")


def _recovered_utf8(text: str) -> str | None:
    try:
        return bytes(_CP1252_TO_BYTE[char] for char in text).decode("utf-8")
    except (KeyError, UnicodeDecodeError):
        return None


def test_source_text_has_no_cp1252_round_trip_damage():
    """A mangled emoji renders as literal garbage in the UI and reads as valid UTF-8.

    The lane header icons hit this: the file stayed well-formed UTF-8, nothing
    failed to load, and the only symptom was a nonsense glyph on the canvas.
    """
    findings = []
    for path in sorted(ROOT.rglob("*")):
        if path.is_dir() or any(part in {".git", "__pycache__", "node_modules"} for part in path.parts):
            continue
        if path.suffix not in {".js", ".py", ".md", ".json", ".toml", ".css", ".html"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for match in _MOJIBAKE.finditer(line):
                recovered = _recovered_utf8(match.group(0))
                if recovered:
                    findings.append(
                        f"{path.relative_to(ROOT)}:{number}: {match.group(0)!r} should be {recovered!r}"
                    )
    assert not findings, "cp1252 round-trip damage in source text:\n" + "\n".join(findings)


def test_source_text_has_no_raw_nul_bytes():
    """A raw NUL makes ripgrep treat the whole file as binary.

    It is silent in both directions: a NUL is valid UTF-8, so nothing fails to
    load and every text tool reads the file fine, while a directory-scoped
    ripgrep SKIPS the file entirely (no output, no warning) and an
    explicit-path search TRUNCATES at the first match past the NUL. That is how
    two `join("\\0")` separators written as literal NULs made 16k lines of
    `editor_widget.js` partially invisible to code search.

    Write the escape (`\\u0000`), never the byte. Bytes are read directly here
    because a NUL cannot be matched from a grep pattern.
    """
    findings = []
    for path in sorted(ROOT.rglob("*")):
        if path.is_dir() or any(part in {".git", "__pycache__", "node_modules"} for part in path.parts):
            continue
        # Same allowlist as the cp1252 check above: tracked .png/.webp/.woff2
        # assets contain NUL by construction and are not source text.
        if path.suffix not in {".js", ".py", ".md", ".json", ".toml", ".css", ".html"}:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        offset = raw.find(b"\x00")
        if offset >= 0:
            line = raw.count(b"\n", 0, offset) + 1
            findings.append(
                f"{path.relative_to(ROOT)}:{line}: raw NUL byte at offset {offset}"
                f" ({raw.count(chr(0).encode())} total) — write \\u0000 instead"
            )
    assert not findings, "raw NUL bytes in source text:\n" + "\n".join(findings)


# The Prompt tool's four colour-bearing modules. Scoped deliberately: the
# editor-wide normalization is its own execution-queue entry and 298 more raw
# values, so widening this list is a decision, not a maintenance chore.
_TOKENIZED_PROMPT_MODULES = (
    "web/js/prompt_context_chips.js",
    "web/js/prompt_identity_panel.js",
    "web/js/editor_prompt_panel.js",
    "web/js/prompt_format_editor.js",
)
# Context-chip role identity has no theme token and deliberately stays literal;
# it lives in one named constant so this guard can name its exception once.
_CHIP_PALETTE_ALLOWLIST = frozenset({
    "#6f62a8", "#29243b", "#ded6ff", "#c9bfff", "#b8a9ef",
})
_RAW_HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def test_prompt_tool_colour_comes_from_theme_tokens():
    """A LINT, not coverage — it proves nothing about how the surface looks.

    What it does prove is that no new raw colour enters these four files, which
    is the invariant that decayed last time: with no token to reach for, a row
    tint was authored as `#c8b48a`, one shade off `statusPending`, for a state
    that is not a status at all. That broke the "pending/orange is status-only"
    rule invisibly, and no behavioural test could have seen it.

    A genuinely new role extends `editor_theme.js`; a genuine one-off joins the
    allowlist above with a comment saying why it is not a token.
    """
    findings = []
    for relative in _TOKENIZED_PROMPT_MODULES:
        path = ROOT / relative
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in _RAW_HEX.finditer(line):
                if match.group(0) in _CHIP_PALETTE_ALLOWLIST:
                    continue
                findings.append(f"{relative}:{number}: raw colour {match.group(0)}")
    assert not findings, (
        "raw colour in the Prompt tool; use editor_theme.js tokens:\n"
        + "\n".join(findings)
    )


def test_reference_tags_do_not_bypass_the_shared_formatter():
    """Reference tag ids must not grow another ad-hoc display spelling."""
    findings = []
    prefix_strip = re.compile(r"replace\(\s*/\^sonder:/")
    direct_join = re.compile(
        r"(?:member|usage)(?:\?\.|\.)tags\s*(?:\?\.)?\.join\("
        r"|(?:member|usage)(?:\?\.|\.)tags\s*\|\|\s*\[\]\s*\)\s*\.join\("
        r"|\btags\s*(?:\?\.)?\.join\(")
    for path in sorted((ROOT / "web" / "js").glob("*.js")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if prefix_strip.search(line):
                findings.append(f"{path.relative_to(ROOT)}:{number}: strips the tag namespace")
            if direct_join.search(line):
                findings.append(f"{path.relative_to(ROOT)}:{number}: joins raw member/usage tags")
    assert not findings, (
        "Reference tags must render through formatReferenceTag:\n"
        + "\n".join(findings)
    )


def test_reference_tag_formatter_is_wired_to_every_display_boundary():
    required = {
        "web/js/editor_reference_library.js": (
            "member.tags.map((tag) => formatReferenceTag",
            "filterReferences(data.references, state.query, allAssets, data.catalog, data.tagFamilies)",
        ),
        "web/js/editor_widget.js": (
            "formatReferenceTag: (tag) => this._formatReferenceTag(tag)",
            "tagged ${this._formatReferenceTag(tag)}",
        ),
        "web/js/editor_timeline_canvas.js": ("formatReferenceTag(tag", 'density: "short"'),
        "web/js/editor_reference_panel.js": (
            "tagWithId(tag)", "tagLabel(tag)", "referenceTagSearchText(tag",
        ),
        "web/js/reference_bridge_shape.js": ("formatReferenceTag(tag", 'density: "short"'),
        "web/js/shared_asset_gallery.js": ("options.formatReferenceTag?.(tag)",),
        "web/js/editor_node_controller.js": ("formatReferenceTag: () => null",),
    }
    findings = []
    for relative, needles in required.items():
        source = (ROOT / relative).read_text(encoding="utf-8")
        for needle in needles:
            if needle not in source:
                findings.append(f"{relative}: missing {needle!r}")
    assert not findings, "shared Reference tag formatter wiring drift:\n" + "\n".join(findings)


def _builtin_reference_recipe_names() -> set[str]:
    """Every recipe name a user can pick, from the one declaration that owns them."""
    text = (ROOT / "server" / "timeline_state.py").read_text(encoding="utf-8")
    start = text.index("REFERENCE_RECIPE_PRESETS = (")
    end = text.index("ALL_REFERENCE_RECIPE_PRESETS")
    return set(re.findall(r'"name":\s*"([^"]+)"', text[start:end]))


def test_reference_recipe_table_lists_every_builtin_preset():
    """The recipe table in docs/references.md duplicates a code declaration.

    It shipped incomplete the first time it was written — `Wan VACE Reference
    Sheet` was silently absent — because the table is hand-maintained and a
    missing row looks exactly like a recipe that does not exist. Choosing a
    recipe is the hard part of Reference setup, so the table earns its keep;
    this makes the drift enforced rather than remembered.

    Adding a preset means adding its row. Removing this table is also a valid
    way to satisfy the test, in which case delete the test with it.
    """
    doc = (ROOT / "docs" / "references.md").read_text(encoding="utf-8")
    missing = sorted(name for name in _builtin_reference_recipe_names() if name not in doc)
    assert not missing, (
        "docs/references.md recipe table is missing built-in preset(s):\n"
        + "\n".join(f"  - {name}" for name in missing)
    )
