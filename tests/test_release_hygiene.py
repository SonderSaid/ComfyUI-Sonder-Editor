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
