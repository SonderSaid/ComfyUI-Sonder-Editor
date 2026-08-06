"""The settings panel talks to an ADAPTER, not the editor.

`_createSettingsPanelHost()` in web/js/editor_widget.js builds a plain object and
forwards a hand-written subset of the editor's surface onto it. A control that
calls a method the adapter never forwarded does not throw loudly — optional
chaining and `|| []` fallbacks mean it renders EMPTY and looks like a styling
bug. That is exactly how the Channel Template picker shipped as a blank
dropdown.

This scans for the mismatch instead of relying on someone remembering the
indirection.
"""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "web" / "js" / "editor_settings_panel.js"
WIDGET = ROOT / "web" / "js" / "editor_widget.js"

# Names the panel reaches through `this.` that the adapter is NOT expected to
# provide: the panel owns them on itself, or they are assigned by the panel.
_PANEL_OWNED = {
    "_settingsPanelEl", "_settingsPanelControls", "_settingsPanelKeyOff",
    "_settingsPanelHandle", "_settingsPanelScrollTop", "_settingsPanelSection",
}


def _adapter_source() -> str:
    """The object literal returned by `_createSettingsPanelHost`."""
    source = WIDGET.read_text(encoding="utf-8")
    start = source.index("_createSettingsPanelHost() {")
    # The adapter ends at the first line that closes the method at class indent.
    end = source.index("\n    }\n", start)
    return source[start:end]


def _panel_host_references() -> set:
    text = PANEL.read_text(encoding="utf-8")
    return {name for name in re.findall(r"\bthis\.(_[A-Za-z0-9_]+)", text)}


def test_every_host_member_the_settings_panel_uses_is_forwarded():
    adapter = _adapter_source()
    referenced = _panel_host_references() - _PANEL_OWNED
    assert referenced, "expected the scan to find real host references"

    missing = sorted(
        name for name in referenced
        # Matches both `name:` entries and `get name()` accessors.
        if not re.search(rf"(?:^|[\s,{{])(?:get\s+)?{re.escape(name)}\s*[(:]", adapter, re.M)
    )
    assert not missing, (
        "settings panel calls host members the adapter never forwards, so they "
        f"render empty instead of failing: {missing}"
    )


def test_the_channel_template_controls_are_forwarded():
    # The specific regression: an unforwarded options getter produced a blank
    # dropdown rather than an error.
    adapter = _adapter_source()
    for name in ("_channelTemplate", "_promptChannelTemplateOptions",
                 "_setPromptChannelTemplate", "_openChannelTemplateEditor"):
        assert name in adapter, name


def test_the_scan_would_actually_catch_a_missing_forward():
    # Anti-vacuity: prove the matcher rejects a name that is not in the adapter.
    adapter = _adapter_source()
    assert not re.search(r"(?:^|[\s,{])(?:get\s+)?_definitelyNotForwarded\s*[(:]",
                         adapter, re.M)
