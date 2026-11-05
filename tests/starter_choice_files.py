"""The model files one Sonder App starter choice needs, worked out offline.

Shared by the starter tests and the setup-page parity test, so both read
"which choice needs which file" from one place.
"""
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def starter_menu(slug):
    """A starter's API JSON, its meta, and its Relay manifest."""
    api = json.loads((ROOT / 'sdk_starters' / (slug + '.json')).read_text(encoding='utf-8'))
    meta = json.loads((ROOT / 'sdk_starters' / (slug + '.meta.json')).read_text(encoding='utf-8'))
    relay = next(n for n in api.values() if n['class_type'] == 'SonderControlRelay')
    return api, meta, json.loads(relay['inputs']['manifest'])


def choice_files(slug, choice_name):
    """Model files a Sonder App run of one Mode choice needs, applied over the
    export's defaults and prepared as Generate prepares it (run_preparation).

    An offline approximation of applying a choice (members' values and switch
    modes straight from the manifest, no describe or _expand); the real build of
    every Task choice is judged by ComfyUI's own validator in the Phase 3 live
    check of the 0.8 hardening.
    """
    from server import control_manifest as cm, control_routing as cr, control_rows, prompt_graph, run_preparation

    api, meta, menu = starter_menu(slug)
    preset = next(p for p in menu['presets'].values() if p['name'] == choice_name)
    graph = copy.deepcopy(api)
    for identity, member in preset['members'].items():
        for target in menu['bindings'][identity]['targets']:
            if isinstance(member['value'], list):
                control_rows.apply_rows(graph[target['path']]['inputs'], target['input'], member['value'])
            else:
                graph[target['path']]['inputs'][target['input']] = member['value']
    modes = {key: entry['mode'] for key, entry in meta['initial_node_modes'].items()}
    modes.update(cm.execution_effects(preset.get('execution', {})))
    modes = {key: mode for key, mode in modes.items() if mode != 'enabled'}
    routing, error = cr.read_routing(menu['execution']['routing'])
    assert error is None
    compiled = cr.compile_modes(graph, routing, modes) if modes else graph
    outputs = {'SonderSaveVideo', 'SonderPreviewVideo'}
    lookup = lambda name: type('Output', (), {'OUTPUT_NODE': True}) if name in outputs else object  # noqa: E731
    prepared = run_preparation.prepare(compiled, class_lookup=lookup)['prompt']
    roots = [key for key, n in prepared.items() if n['class_type'] in outputs]
    needed = prompt_graph.upstream_set(prepared, roots)
    return {value.replace('\\', '/').rsplit('/', 1)[-1] for key in needed
            for value in prepared[key]['inputs'].values()
            if isinstance(value, str) and value.endswith('.safetensors')}
