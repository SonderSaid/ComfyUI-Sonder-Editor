"""Inline prompt bars must commit on focus loss, not only on Enter.

Source-level contract checks for the fullscreen prompt bars in
`editor_widget.js`: every close path flushes the pending edit through one
hook, and only the explicit discard paths (Esc, Cancel, delete) skip it.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _declaration(source: str, name: str, from_index: int = 0) -> int:
    for prefix in (f"    {name}(", f"    async {name}("):
        found = source.find(prefix, from_index)
        if found >= 0:
            return found
    raise AssertionError(f"method {name} not found in source")


def _method(source: str, name: str, next_name: str) -> str:
    start = _declaration(source, name)
    return source[start:_declaration(source, next_name, start)]


def test_hide_flushes_pending_edit_and_clears_the_hook_first():
    hide = _method(_source("web/js/editor_widget.js"), "_hidePromptEditor", "_updatePromptSection")

    assert "_hidePromptEditor({ commit = true } = {})" in hide
    # The hook is read and cleared before it runs, so a flush whose commit
    # closes the bar cannot re-enter and double-write. `_promptEditorEl` is
    # dropped before the removal, so the focusout that removal fires finds no
    # mounted bar and cannot resurrect a discarded edit.
    clear_at = hide.index("this._promptEditorCommit = null")
    run_at = hide.index("if (commit && pendingCommit) pendingCommit()")
    unmount_at = hide.index("this._promptEditorEl = null")
    remove_at = hide.index("editorEl.remove()")
    assert clear_at < run_at < unmount_at < remove_at


def test_section_bar_commits_on_focus_loss_and_dedupes_unchanged_channels():
    editor = _method(_source("web/js/editor_widget.js"), "_showPromptEditor", "_showGlobalPromptEditor")

    assert 'editor.addEventListener("focusout"' in editor
    # A bar that is no longer mounted never writes, so the focusout its own
    # teardown fires cannot undo an Esc or a Delete.
    assert "if (this._promptEditorEl !== editor) return;" in editor
    # Moving focus inside the bar (Save/Delete) is not a commit trigger.
    assert "if (e.relatedTarget && editor.contains(e.relatedTarget)) return;" in editor
    assert "commit({ close: false })" in editor
    assert "this._promptEditorCommit = () => commit({ close: false });" in editor
    # Only a real text change reaches the mutation pipeline, so repeated focus
    # changes cannot stack no-op writes or undo entries.
    assert "next.visual !== committed.visual" in editor
    assert "committed = next;" in editor
    # Esc discards; Delete arms before the blur it causes.
    assert "this._hidePromptEditor({ commit: false });" in editor
    assert 'deleteBtn.addEventListener("mousedown", () => { discard = true; });' in editor


def test_creator_bar_creates_on_focus_loss_but_never_twice():
    creator = _method(_source("web/js/editor_widget.js"), "_showPromptCreator", "_saveNewPromptSection")

    assert 'editor.addEventListener("focusout"' in creator
    # A bar that is no longer mounted never creates, so Cancel and Esc cannot
    # be turned into a create by their own teardown.
    assert "if (this._promptEditorEl !== editor) return;" in creator
    assert "this._promptEditorCommit = commit;" in creator
    # The save path hides, and hiding flushes — `created` stops the second run.
    assert "if (created || discard) return;" in creator
    assert "created = true;" in creator
    # Cancel arms before the blur it causes, then discards explicitly.
    assert 'cancelBtn.addEventListener("mousedown", () => { discard = true; });' in creator
    assert 'cancelBtn.addEventListener("click", () => this._hidePromptEditor({ commit: false }));' in creator


def test_global_bar_registers_the_same_flush_hook():
    widget = _source("web/js/editor_widget.js")
    global_bar = _method(widget, "_showGlobalPromptEditor", "_updateScenePrompt")

    assert "this._promptEditorCommit = commit;" in global_bar
    assert "this._hidePromptEditor({ commit: false });" in global_bar


def test_discarding_close_paths_do_not_write_stale_edits():
    widget = _source("web/js/editor_widget.js")

    # A deleted section's index is gone and the ones behind it have shifted.
    delete_section = _method(widget, "_deletePromptSection", "_showItemEditor")
    assert "this._hidePromptEditor({ commit: false });" in delete_section

    # Leaving fullscreen flushes before the mutation drain, not after.
    exit_fullscreen = _method(widget, "_requestExitFullscreen", "_exitFullscreen")
    flush_at = exit_fullscreen.index("this._hidePromptEditor();")
    drain_at = exit_fullscreen.index("await this._drainProjectMutations(")
    assert flush_at < drain_at
