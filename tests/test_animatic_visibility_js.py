from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _method(source: str, name: str, next_name: str) -> str:
    def _start(method_name: str, offset: int = 0) -> int:
        candidates = [
            source.find(f"    {method_name}(", offset),
            source.find(f"    async {method_name}(", offset),
        ]
        return min(candidate for candidate in candidates if candidate >= 0)

    start = _start(name)
    end = _start(next_name, start + 1)
    return source[start:end]


def test_animatic_toggle_is_ephemeral_and_does_not_write_lane_config():
    widget = _source("web/js/editor_widget.js")
    toggle = _method(widget, "_toggleAnimatic", "_buildTrackLayout")
    scene_change = _method(widget, "_setActiveScene", "_refreshDurationInput")

    assert "this._animaticMode = !this._animaticMode;" in toggle
    assert "_saveLaneConfig" not in toggle
    assert "entry.hidden" not in toggle
    assert "_preAnimaticHidden" not in widget
    assert "this._animaticMode = false;" in scene_change
    assert "_saveLaneConfig" not in scene_change


def test_animatic_visibility_is_derived_for_video_only():
    widget = _source("web/js/editor_widget.js")
    is_hidden = _method(widget, "_isLaneHidden", "_muteOperationForItem")
    visibility_state = _method(widget, "_trackVisibilityState", "_isGuideTrackLocked")

    assert "this._animaticMode && type === TRACK_TYPE.VIDEO" in is_hidden
    assert "this._isLaneVisibilityControlDisabled(entry)" in visibility_state


def test_timeline_uses_effective_animatic_visibility():
    timeline = _source("web/js/editor_timeline_canvas.js")

    assert "const laneHidden = host._isLaneHidden(_vlEntry.type, _vlEntry.laneIndex);" in timeline
    assert "const laneHidden = _vlEntry.hidden;" not in timeline


def test_video_visibility_controls_are_disabled_during_animatic():
    widget = _source("web/js/editor_widget.js")
    setup_events = _method(widget, "_setupTimelineEvents", "_canvasMouseCoords")
    toggle_header = _method(widget, "_toggleHeaderVisibility", "_isLaneVisibilityControlDisabled")
    disabled = _method(widget, "_isLaneVisibilityControlDisabled", "_applyHeaderVisibilityBulk")
    apply_bulk = _method(widget, "_applyHeaderVisibilityBulk", "_startLaneRename")

    assert "if (this._isLaneVisibilityControlDisabled(entry)) break;" in setup_events
    assert "!this._isLaneVisibilityControlDisabled(target)" in setup_events
    assert "if (this._isLaneVisibilityControlDisabled(entry)) return;" in toggle_header
    assert "entry?.type === TRACK_TYPE.VIDEO" in disabled
    assert "!this._isLaneVisibilityControlDisabled(entry)" in apply_bulk
