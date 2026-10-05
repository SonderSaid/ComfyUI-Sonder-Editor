"""SDK seeds advance exactly as the installed ComfyUI frontend advances a seed widget.

Runs the installed bundle's own ``computeNextNumberValue`` in node, bound to the
bundle's own +-2^50 limit constants, and compares it with
``control_descriptors.next_seed_value`` on shared cases, including ``step``
(which the frontend reads as ``options.step2``). Skips when node or the
frontend package is absent.
"""

import glob
import importlib.util
import json
import os
import random
import re
import shutil
import subprocess

import pytest

from server.control_descriptors import next_seed_value

def _bundle_source():
    spec = importlib.util.find_spec("comfyui_frontend_package")
    if spec is None or not spec.submodule_search_locations:
        return None
    root = list(spec.submodule_search_locations)[0]
    for path in glob.glob(os.path.join(root, "static", "assets", "*.js")):
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        if "function computeNextNumberValue(" in text:
            return text
    return None


def _extract(text):
    start = text.index("function computeNextNumberValue(")
    # The function body ends at the first "}" that closes its outer brace.
    depth, index = 0, text.index("{", start)
    for index in range(index, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                break
    function = text[start:index + 1]
    # Bind the module-level limit constants the function reads (minified names);
    # local variables such as the value itself are skipped by the magnitude test.
    names = set(re.findall(r"Math\.(?:min|max)\((\w+),", function))
    constants = {}
    for name in names:
        match = re.search(rf"\b{re.escape(name)}=(-?0x[0-9a-fA-F]+|-?\d+)\b", text)
        if match and abs(int(match.group(1), 0)) == 2 ** 50:
            constants[name] = int(match.group(1), 0)
    return function, constants


CASES = [
    ("increment", 10, 0, 100, 1),
    ("increment", 100, 0, 100, 1),          # clamps at max
    ("decrement", 0, 0, 100, 1),            # clamps at min
    ("decrement", 50, 0, 100, 5),
    ("increment-wrap", 7, 0, 100, 1),        # a number: acts as increment
    ("increment", 5, 0, 2 ** 64 - 1, 1),     # bounds beyond +-2^50 are clamped to it
    ("randomize", 5, 0, 2 ** 32 - 1, 1),
    ("randomize", 5, 0, 2 ** 64 - 1, 1),
    ("randomize", 5, 10, 20, 2),
    ("fixed", 5, 0, 100, 1),
]
RANDOMS = [0.0, 0.25, 0.5, 0.999999]


def test_next_seed_value_matches_the_installed_frontend():
    node = shutil.which("node")
    text = _bundle_source()
    if node is None or text is None:
        pytest.skip("node or comfyui_frontend_package not available")
    function, constants = _extract(text)
    assert len(constants) == 2, constants  # the +-2^50 limits

    cases = []
    for mode, value, lo, hi, step in CASES:
        for r in RANDOMS if mode == "randomize" else [0.0]:
            cases.append({"mode": mode, "value": value, "min": lo, "max": hi, "step": step, "r": r})
    script = (
        "".join(f"const {name}={value};" for name, value in constants.items())
        + function
        + f"const cases={json.dumps(cases)};"
        + "const out=cases.map(c=>{Math.random=()=>c.r;"
        + "const w={value:c.value,options:{min:c.min,max:c.max,step2:c.step}};"
        + "const mode=c.mode==='fixed'?'none':c.mode;"
        + "const v=c.mode==='fixed'?c.value:computeNextNumberValue(w,mode);return v;});"
        + "console.log(JSON.stringify(out));"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    frontend = json.loads(result.stdout)

    for case, expected in zip(cases, frontend):
        rng = random.Random()
        rng.random = lambda r=case["r"]: r
        ours = next_seed_value(case["mode"], case["value"], minimum=case["min"], maximum=case["max"],
                               step=case["step"], rng=rng)
        assert ours == expected, case
