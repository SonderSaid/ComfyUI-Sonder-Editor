"""Keep unit-test libraries independent of the installed pack's starter files."""

import sys

import pytest


@pytest.fixture(autouse=True)
def isolate_bundled_starters(monkeypatch, tmp_path_factory):
    # SDK tests use synthetic class registries and workflow libraries. Tests
    # of starter sources select their own fixture pack after this isolation.
    templates = sys.modules.get("server.sdk_templates")
    if templates is not None:
        monkeypatch.setattr(templates, "STARTERS_DIR", str(tmp_path_factory.getbasetemp() / "no-pack-starters"))
