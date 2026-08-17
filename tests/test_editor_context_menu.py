from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_shared_context_menu_owns_one_accessible_nested_stack():
    source = _source("web/js/editor_context_menu.js")
    assert source.count("registerKeyboardConsumer({") == 1
    assert 'row.tabIndex = -1' in source
    assert 'row.setAttribute("aria-haspopup", "menu")' in source
    assert 'event.key === "ArrowDown"' in source
    assert 'event.key === "ArrowUp"' in source
    assert 'event.key === "ArrowRight"' in source
    assert 'event.key === "ArrowLeft"' in source
    assert 'event.key === "Home"' in source
    assert 'event.key === "End"' in source
    assert 'event.key === "Enter"' in source
    assert 'event.key === "Escape"' in source
    assert "left + rect.width > window.innerWidth - 4" in source
    assert 'element.dataset.sonderPromptContextMenu = "1"' in source
    assert 'event.preventDefault();' in source


def test_context_menu_ime_short_circuit_is_falsy_and_prompt_open_is_refused():
    menu = _source("web/js/editor_context_menu.js")
    chips = _source("web/js/prompt_context_chips.js")
    assert "if (isComposing(event)) return false;" in menu
    assert "event?.isComposing === true || event?.keyCode === 229" in menu
    assert "if (editor.isPromptComposing?.()) return false;" in chips
    assert "event.isComposing || event.keyCode === 229) return false" in chips


def test_widget_and_gallery_use_the_shared_menu_without_local_forks():
    widget = _source("web/js/editor_widget.js")
    gallery = _source("web/js/shared_asset_gallery.js")
    assert "openContextMenu({ x, y, items })" in widget
    assert "openContextMenu({ x, y, items, closeOnScroll: true })" in gallery
    assert "LegacyContextMenu" not in widget
    assert "LegacyContextMenu" not in gallery


def test_prompt_menu_preserves_caret_focus_and_surface_specific_commit_contracts():
    chips = _source("web/js/prompt_context_chips.js")
    panel = _source("web/js/editor_prompt_panel.js")
    widget = _source("web/js/editor_widget.js")
    assert "caretRangeFromPoint" in chips and "caretPositionFromPoint" in chips
    assert "promptInsertionBookmark(editor)" in chips
    assert "restorePromptInsertion(editor, bookmark)" in chips
    assert 'label: "Insert at cursor"' in chips
    assert 'label: "Writing aid"' in chips
    assert 'onInserted: async () => { await onEnter?.({ close: false }); }' in widget
    assert 'onInserted: ({ type }) => type === "writing_aid" ? commitGlobal() : null' in panel
    assert 'onInserted: ({ type }) => type === "writing_aid" ? commitChannels() : null' in panel
    assert "createContextPicker" not in chips + panel + widget
    assert "data-sonder-prompt-context-menu='1'" in panel
    assert "data-sonder-prompt-context-menu='1'" in widget


def test_all_channels_is_the_new_browser_local_default_and_keeps_every_editor_mounted():
    widget = _source("web/js/editor_widget.js")
    settings = _source("web/js/editor_settings.js")
    assert 'allOption.value = "__all__"' in widget
    assert 'allOption.textContent = "All channels"' in widget
    assert 'sonder.prompt.channelView.v2.' in widget
    assert 'sonder.prompt.activeChannel.' not in widget
    assert '? remembered : "__all__"' in widget
    assert 'key !== "__all__" && !keys.includes(key)' in widget
    assert 'showAll ? "1 1 220px" : "1 0 100%"' in widget
    assert 'if (activeKey === "__all__")' in widget
    assert "const syncAllAttachments" not in widget
    assert 'inputs[keys.includes(activeKey) ? activeKey : keys[0]]?.focus()' in widget
    assert "contextMenuHintDismissed: false" in settings
    assert "contextMenuHintDismissed: raw.contextMenuHintDismissed" in settings
    assert 'this._shortcutSection("Prompt"' in widget
    assert '"Shift+F10 / Menu"' in widget


def test_menu_rows_bound_declared_hint_text_and_keep_the_arrow_rightmost():
    """A hint comes from declared metadata, so the renderer owns its bound."""
    source = _source("web/js/editor_context_menu.js")
    # The panel cannot grow without limit just because a declaration is long.
    assert "max-width:min(440px,92vw);" in source
    assert "hint.length > 48" in source
    assert "if (hint.length > 48) secondary.title = hint;" in source
    # Hint and arrow share one trailing group rather than competing for the
    # same flex end, so a row carrying both stays readable.
    assert 'trailing.appendChild(secondary)' in source
    assert 'trailing.appendChild(arrow)' in source


def test_writing_aid_rows_preview_the_text_they_insert():
    chips = _source("web/js/prompt_context_chips.js")
    # The preview is the aid's own insertable text. A capability's `example` is
    # presentation-only and must never be offered as if it were insertable.
    assert "function writingAidPreview(aid)" in chips
    assert "hint = writingAidPreview(aid)" in chips
    assert "example" not in chips.split("function writingAidPreview")[1].split("}")[0]
