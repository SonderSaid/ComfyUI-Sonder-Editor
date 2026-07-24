from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _method(source: str, name: str, next_name: str) -> str:
    start = source.index(f"    {name}(")
    end = source.index(f"    {next_name}(", start)
    return source[start:end]


def test_text_fields_shortcuts_and_steppers_share_manual_commit_path():
    widget = _source("web/js/editor_widget.js")
    chrome = _source("web/js/editor_top_chrome.js")

    assert '_commitManualSelectionEndpoint("start", frame)' in chrome
    assert '_commitManualSelectionEndpoint("end", frame)' in chrome
    assert '_commitManualSelectionEndpoint("start", this.playhead)' in widget
    assert '_commitManualSelectionEndpoint("end", this.playhead)' in widget
    stepper = _method(widget, "_stepSelectionInput", "_refreshSelectionInputs")
    assert "this._commitManualSelectionEndpoint(edge" in stepper
    assert "searchDirection: coordinateDirection" in stepper
    assert "_snapSelectionFrame" not in widget
    assert "_snapSelectionFrame" not in chrome


def test_guide_endpoint_actions_share_manual_commit_path():
    widget = _source("web/js/editor_widget.js")
    set_start = _method(widget, "_setSelectionStartFrame", "_setSelectionEndFrame")
    set_end = _method(widget, "_setSelectionEndFrame", "_clearTimelineSelection")

    assert 'this._commitManualSelectionEndpoint("start", frame);' in set_start
    assert 'this._commitManualSelectionEndpoint("end", frame);' in set_end
    assert "_setTimelineSelection" not in set_start
    assert "_setTimelineSelection" not in set_end


def test_draft_is_ephemeral_and_crossing_restarts_it():
    widget = _source("web/js/editor_widget.js")
    draft = _method(widget, "_setSelectionDraft", "_commitManualSelectionEndpoint")
    commit = _method(widget, "_commitManualSelectionEndpoint", "_setSelectionStartFrame")

    assert "this._selectionDraftAnchor = null" in widget
    assert "this._setTimelineSelection(0, 0" in draft
    assert "this._selectionDraftAnchor = { edge: nextEdge, frame: nextFrame }" in draft
    assert "!hasSelection && (!draft || draft.edge === nextEdge)" in commit
    assert "if (hasSelection)" in commit
    assert "this._setSelectionDraft(nextEdge, candidate)" in commit
    assert "this._setTimelineSelection(start, end)" in commit


def test_draft_cleanup_and_exact_range_paths_are_explicit():
    widget = _source("web/js/editor_widget.js")
    apply_state = _method(widget, "applyWidgetState", "_flushDeferredDragState")
    scene_change = _method(widget, "_setActiveScene", "_refreshDurationInput")
    context_change = _method(widget, "_updateContextFrameWidgets", "_stepContextFrameInput")

    assert "this._selectionDraftAnchor = null" in apply_state
    assert "this._selectionDraftAnchor = null" in scene_change
    assert "if (this._selectionDraftAnchor)" in widget[widget.index("async _handleTemplateSelectionChange"):]
    assert "_selectionDraftAnchor = null" not in context_change
    assert "_setSelectionToFrameRange(start, end) {\n        this._setTimelineSelection(start, end);" in widget
    assert "_recallSavedSelection(sel) {\n        this._setTimelineSelection(sel.start, sel.end" in widget


def test_draft_marker_readout_and_action_guards_are_present():
    widget = _source("web/js/editor_widget.js")
    chrome = _source("web/js/editor_top_chrome.js")
    timeline = _source("web/js/editor_timeline_canvas.js")

    assert "Choose ${next}" in widget
    assert "frame_count_padding" in widget
    assert "_selectionDraftAnchor" in timeline
    assert 'draft.edge === "start" ? "In" : "Out"' in timeline
    assert "widget._queueBtn.disabled = !!draft" in chrome
    assert "widget._batchQueueBtn.disabled = !!draft" in chrome
    assert "selection-draft-save-guard" in widget
    assert "selection-draft-queue-guard" in widget
    assert "selection-draft-batch-guard" in widget
    assert "if (!this.activeScene || this._selectionDraftAnchor) return null;" in widget


def test_batch_tail_partitioning_remains_exact():
    widget = _source("web/js/editor_widget.js")
    batch = _method(widget, "_buildBatchQueueRanges", "_updateBatchButtonLabel")

    assert "const nextEnd = Math.min(cursor + thisSize, end);" in batch
    assert "ranges.push({ start: cursor, end: nextEnd });" in batch


def test_empty_selection_uses_legends_instead_of_zero_values():
    widget = _source("web/js/editor_widget.js")
    refresh = _method(widget, "_refreshSelectionInputs", "_refreshPlayheadInput")

    assert "const hasSelection = this.selectionStart < this.selectionEnd;" in refresh
    assert ': (hasSelection ? this._formatPositionInput(this.selectionStart) : "")' in refresh
    assert ': (hasSelection ? this._formatPositionInput(this.selectionEnd) : "")' in refresh
    assert '? "Set In" : ""' in refresh
    assert '? "Set Out" : ""' in refresh


def test_playhead_mouseup_does_not_clear_keyboard_draft():
    widget = _source("web/js/editor_widget.js")
    mouseup_start = widget.index("        const onMouseUp = (e) => {")
    mouseup_end = widget.index('        canvas.addEventListener("mouseup", onMouseUp);', mouseup_start)
    mouseup = widget[mouseup_start:mouseup_end]

    playhead_branch = mouseup.index('wasDragType === "playhead"')
    selection_write = mouseup.index("this._setTimelineSelection(this.selectionStart, this.selectionEnd")
    assert playhead_branch < selection_write
    assert "Ruler navigation must not finalize or clear" in mouseup


def test_duration_hint_uses_constraint_aware_tolerance():
    widget = _source("web/js/editor_widget.js")
    hint = _method(widget, "_updateGenDurationHint", "_readStoredTimelineSelection")

    assert "isSelectionDurationWithinRecommendation" in hint
    assert "frameConstraint: this._getActiveFrameConstraint()" in hint
