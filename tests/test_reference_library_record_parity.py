"""A Library create is painted as the record the route stores.

Since Library paint-first Phase 3 a new Reference or member is painted under
the id the client minted, and it is usable at once: an Edit takes its base from
the painted row, a Remove sends the painted member as `delete_member`'s
whole-record guard, a Delete sends the painted Reference's fields. Each of
those is compared by the route against what it stored, so the paint must be
that record, key for key -- `reference_library_model.js`'s
`referenceCreateRecord` and `referenceMemberCreateRecord` mirror
`_apply_create_reference` and `_member_from_fields` + `to_dict`, and a minted
id must have the shape `_client_library_id` accepts.

Inputs are the fields a caller sends that the route accepts; the mirrors do not
reproduce the route's refusals, which the write still makes.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from server import routes
from server.timeline_state import Asset, ReferenceEntity, TimelineProject

ROOT = Path(__file__).resolve().parents[1]
MODEL = (ROOT / "web" / "js" / "reference_library_model.js").as_uri()


def _node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the Reference Library parity tests")
    completed = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=60)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _project():
    project = TimelineProject(project_id="parity", name="Parity")
    project.assets = [
        Asset(asset_id="image-1", name="Portrait", asset_type="image", path="media/p.png"),
        Asset(asset_id="audio-1", name="Voice", asset_type="audio", path="media/v.wav"),
        Asset(asset_id="video-1", name="Clip", asset_type="video", path="media/c.mp4", has_audio=True),
    ]
    project.references = [ReferenceEntity(reference_id="ref-1", name="Held")]
    return project


REFERENCE_FIELDS = [
    {"name": "Lead", "kind": "character"},
    {"name": "  Padded  ", "kind": "location"},
    {"name": "Lot", "kind": "location", "reference_class": "subject", "description": " keeps spaces "},
    {"name": "Prop", "kind": "prop", "reference_class": "context", "description": "",
     "visual_intent": "partial", "audio_intent": "copy_full"},
    # What the Library's entity form sends (`serializeReferenceDraft`).
    {"name": "Outfit", "kind": "outfit", "reference_class": "subject", "description": "Blue coat",
     "visual_intent": "preserve", "audio_intent": "reference_characteristics"},
]

MEMBER_FIELDS = [
    # What the Library's member form sends (`serializeMemberDraft`).
    {"asset_id": "image-1", "name": "Front", "tags": ["sonder:portrait", "Custom"], "prompt": "a face",
     "crop": {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.6}, "source_start_sec": 0, "source_end_sec": None},
    {"asset_id": "audio-1", "name": "", "tags": [], "prompt": "", "crop": None,
     "source_start_sec": 1.5, "source_end_sec": 4},
    {"asset_id": "video-1", "name": "  Take  ", "tags": ["  Two   words ", "two words", "Other"],
     "prompt": " kept as sent ", "crop": None, "source_start_sec": 0.25, "source_end_sec": ""},
    {"asset_id": "image-1"},
    {"asset_id": "image-1", "handle": " Lead_1 ", "visual_intent": "partial", "audio_intent": "",
     "attachment_defaults": {}, "disabled_capabilities": [" summary ", "summary", "", "definitions"]},
]


def test_a_created_reference_is_painted_as_the_route_stores_it():
    expected = [routes._apply_create_reference(_project(), dict(fields),
                                               reference_id="a" * 32).to_dict()
                for fields in REFERENCE_FIELDS]
    painted = _node(f"""
        import * as model from {MODEL!r};
        const cases = {json.dumps(REFERENCE_FIELDS)};
        console.log(JSON.stringify(cases.map((fields) => model.referenceCreateRecord(fields, 'a'.repeat(32)))));
    """)
    assert painted == expected


def test_a_created_member_is_painted_as_the_route_stores_it():
    expected = [routes._member_from_fields(_project(), dict(fields), member_id="b" * 32,
                                           order=order).to_dict()
                for order, fields in enumerate(MEMBER_FIELDS)]
    painted = _node(f"""
        import * as model from {MODEL!r};
        const cases = {json.dumps(MEMBER_FIELDS)};
        console.log(JSON.stringify(cases.map((fields, order) =>
            model.referenceMemberCreateRecord(fields, 'b'.repeat(32), order))));
    """)
    assert painted == expected


def test_a_minted_library_id_has_the_shape_the_route_accepts():
    minted = _node(f"""
        import * as model from {MODEL!r};
        const ids = Array.from({{ length: 64 }}, () => model.mintReferenceLibraryId());
        // Without Web Crypto, the bounded fallback.
        const crypto = globalThis.crypto;
        Object.defineProperty(globalThis, 'crypto', {{ value: undefined, configurable: true }});
        ids.push(model.mintReferenceLibraryId());
        Object.defineProperty(globalThis, 'crypto', {{ value: crypto, configurable: true }});
        console.log(JSON.stringify(ids));
    """)
    assert len(set(minted)) == len(minted)
    for value in minted:
        assert re.fullmatch(r"[0-9a-f]{32}", value)
        assert routes._CLIENT_ID_PATTERN.match(value)
        assert routes._client_library_id(value, set(), "Reference") == value
