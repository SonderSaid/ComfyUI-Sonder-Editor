"""A legacy project fixture: what Sonder wrote before new projects were created at
the current storage format, a ``project.json`` with no storage block."""
import json
import shutil
from pathlib import Path

from server import project_manager as pm


def create_legacy_project(name, base_dir, **kwargs):
    project = pm.create_project(name, base_dir=str(base_dir), **kwargs)
    directory = Path(project.project_dir)
    path = directory / "project.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("storage", None)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    # The empty history component the new-format save published: a legacy
    # project's state/ folder starts empty.
    shutil.rmtree(directory / "state")
    (directory / "state").mkdir()
    return pm.load_project(project.project_dir)
