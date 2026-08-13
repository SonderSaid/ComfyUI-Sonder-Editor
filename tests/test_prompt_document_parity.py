"""Intentional Python/JavaScript PromptDocument normalizer parity."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server import prompt_context


ROOT = Path(__file__).resolve().parents[1]


def _javascript_normalize(fixtures):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for PromptDocument parity")
    module_url = (ROOT / "web" / "js" / "prompt_context_chips.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
const fixtures = {json.dumps(fixtures)};
console.log(JSON.stringify(fixtures.map((value) =>
  mod.normalizePromptDocument(value.raw, value.fallback))));
"""
    return json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)


def _rename_ids_bijectively(document):
    mapping = {}
    result = json.loads(json.dumps(document))
    for node in result["nodes"]:
        node_id = node["node_id"]
        mapping.setdefault(node_id, f"generated-{len(mapping) + 1}")
        node["node_id"] = mapping[node_id]
    assert len(set(mapping.values())) == len(mapping)
    return result


def test_valid_prompt_document_ids_match_exactly_across_languages():
    fixtures = [{
        "raw": {"schema": 1, "nodes": [
            {"type": "text", "node_id": "text-1", "text": "hello "},
            {"type": "attachment", "node_id": "chip-1",
             "attachment_id": "attachment", "capability_id": "summary"},
            {"type": "text", "node_id": "text-2", "text": "world"},
        ]},
        "fallback": "unused",
    }]
    python = [prompt_context.normalize_prompt_document(
        value["raw"], value["fallback"]) for value in fixtures]
    assert _javascript_normalize(fixtures) == python


def test_generated_prompt_document_ids_match_modulo_bijective_renaming():
    fixtures = [
        {"raw": None, "fallback": "fallback"},
        {"raw": {"nodes": [
            {"type": "text", "node_id": "duplicate", "text": "a"},
            {"type": "text", "node_id": "duplicate", "text": "b"},
            {"type": "attachment", "node_id": "", "attachment_id": "chip"},
            {"type": "unknown", "node_id": "ignored"},
        ]}, "fallback": "unused"},
    ]
    python = [prompt_context.normalize_prompt_document(
        value["raw"], value["fallback"]) for value in fixtures]
    javascript = _javascript_normalize(fixtures)
    assert [_rename_ids_bijectively(value) for value in javascript] == [
        _rename_ids_bijectively(value) for value in python]
