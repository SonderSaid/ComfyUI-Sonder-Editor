from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMFY_OWNED_PACKAGES = {"torch", "torchaudio"}


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
