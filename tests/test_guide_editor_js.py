from pathlib import Path


def _guide_manager_source() -> str:
    root = Path(__file__).resolve().parents[1]
    widget = (root / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    start = widget.index("    _showGuideManagementPopup(x, y) {")
    end = widget.index("    _hideGuideManagementPopup() {", start)
    return widget[start:end]


def test_frame_change_refreshes_guide_manager_instead_of_closing_it():
    manager = _guide_manager_source()
    commit_start = manager.index("            const commitFrameInput = async () => {")
    commit_end = manager.index("            frameInput.addEventListener", commit_start)
    commit = manager[commit_start:commit_end]

    assert "await this._moveGuideToFrame(guide, clamped, guide.strength);" in commit
    assert "await refreshPanel();" in commit
    assert "this._hideGuideManagementPopup();" not in commit


def test_guide_editor_thumbnails_contain_source_without_cropping():
    root = Path(__file__).resolve().parents[1]
    widget = (root / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    item_editor_start = widget.index("    _showItemEditor() {")
    item_editor_end = widget.index("    _hideItemEditor() {", item_editor_start)
    item_editor = widget[item_editor_start:item_editor_end]
    manager = _guide_manager_source()

    assert "object-fit:contain" in item_editor
    assert manager.count("object-fit:contain") >= 1
    assert "object-fit:cover" not in manager
