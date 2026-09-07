"""Guard the toolbar's wrap order and deliberate project-template switch path."""
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
CHROME = (ROOT / "web/js/editor_top_chrome.js").read_text(encoding="utf-8")
WIDGET = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
SETTINGS = (ROOT / "web/js/editor_settings_panel.js").read_text(encoding="utf-8")


def _body(source, start, end):
    return source.split(start, 1)[1].split(end, 1)[0]


def _last_cluster(source):
    append = _body(source, "widget._sceneGeometryGroup.append(", "\n    );")
    return bool(re.search(r"templatesGroup\s*$", append))


def _button(source):
    return 'widget._channelTemplateBtn = document.createElement("button")' in source


def _menu_route(source):
    menu = _body(source, "    _openChannelTemplateMenu(anchorEl) {", "    async _savePromptChannelTemplate")
    return ("openContextMenu({" in menu and "focusFirst: true" in menu
            and "this._setPromptChannelTemplate(option.template)" in menu
            and "_queuePromptProjectWrite" not in menu)


def _toolbar_refresh(source):
    return "refreshChannelTemplateControl(widget);" in source.split("export function updateEditorToolbar(widget) {", 1)[1]


def _project_refresh(source):
    sync = _body(source, "    _syncSceneResolutionControls({", "    _contextFrameValue(")
    return bool(re.search(r"this\._rebuildTemplateOptions\(\);\s*refreshChannelTemplateControl\(this\);", sync))


@pytest.mark.parametrize("source,matcher,old,new", [
    (CHROME, _last_cluster, "        templatesGroup\n", "        widget._templateSelect\n"),
    (CHROME, _button, '_channelTemplateBtn = document.createElement("button")', '_channelTemplateBtn = document.createElement("select")'),
    (WIDGET, _menu_route, "this._setPromptChannelTemplate(option.template)", "this._queuePromptProjectWrite(option.template)"),
    (WIDGET, _menu_route, "items, focusFirst: true", "items, focusFirst: false"),
    (CHROME, _toolbar_refresh, "refreshChannelTemplateControl(widget);", "refreshOtherControl(widget);"),
    (WIDGET, _project_refresh, "refreshChannelTemplateControl(this);", "refreshOtherControl(this);"),
], ids=["wrap-order", "button", "guarded-write", "menu-focus", "toolbar-refresh", "project-refresh"])
def test_contract_and_negative_control(source, matcher, old, new):
    assert matcher(source)
    assert old in source
    assert not matcher(source.replace(old, new))


def test_pair_captions_and_refresh_use_project_value():
    assert 'makeInlineGroup(modelLabel, widget._templateSelect, channelsLabel, widget._channelTemplateBtn)' in CHROME
    assert 'modelLabel.textContent = "Model:"' in CHROME
    assert 'channelsLabel.textContent = "Channels:"' in CHROME
    refresh = _body(CHROME, "export function refreshChannelTemplateControl(widget)", "export function updateEditorToolbar")
    assert "widget._channelTemplate()" in refresh
    assert "widget._channelTemplateLabel.textContent = template.name" in refresh
    assert "project-wide channels:" in refresh
    assert "button.dataset.sonderChannelTemplate = template.id" in refresh
    switch = _body(WIDGET, "    async _setPromptChannelTemplateWithinGesture(", "    _promptChannelTemplateOptions()")
    save = _body(WIDGET, "    async _savePromptChannelTemplate(", "    async _adoptPromptChannelTemplate(")
    assert "refreshChannelTemplateControl(this)" in switch
    assert "refreshChannelTemplateControl(this)" in save


def test_manage_targets_existing_panel_section():
    menu = _body(WIDGET, "    _openChannelTemplateMenu(anchorEl) {", "    async _savePromptChannelTemplate")
    assert "this._showSettingsPanel();" in menu
    assert 'data-sonder-settings-section="channel-templates"' in menu
    assert 'scrollIntoView({ block: "start" })' in menu
    assert 'channelTemplatesSection.dataset.sonderSettingsSection = "channel-templates"' in SETTINGS
