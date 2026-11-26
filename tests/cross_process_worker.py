"""The second process of ``test_project_cross_process.py``; never imported by it.

``python cross_process_worker.py <flags dir> <role> <project dir> [args…]``. Barriers
are flag files. One that is not raised in time exits non-zero, so a barrier that could
not be met fails the test instead of letting it pass on an ordering it never forced. The
result goes to a file: importing ``server`` can log to stdout.
"""
import inspect
import json
import os
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import project_file_lock as file_lock, project_manager as pm, project_storage as ps  # noqa: E402
from server import server_settings  # noqa: E402
from server.timeline_state import Asset  # noqa: E402

FLAGS = sys.argv[1]
BARRIER_SECONDS = 180  # the other process may still be importing on a busy machine


def raise_flag(name):
    with open(os.path.join(FLAGS, name), "w"):
        pass


def wait_flag(name):
    deadline = time.monotonic() + BARRIER_SECONDS
    while not os.path.exists(os.path.join(FLAGS, name)):
        if time.monotonic() > deadline:
            raise SystemExit(f"barrier {name} was never met")
        time.sleep(0.005)


def report(value):
    # Named by the test: in a Windows venv this process is not the one it started.
    with open(os.environ["CROSS_PROCESS_RESULT"], "w") as handle:
        json.dump(value, handle)


def collect(project_dir, **kwargs):
    if "retention_seconds" not in inspect.signature(ps.collect_unreferenced_components).parameters:
        kwargs = {}  # the code before retention existed, so the races can be shown failing on it
    return ps.collect_unreferenced_components(project_dir, **kwargs)


def generated(asset_id, seed):
    return Asset(asset_id=asset_id, name=asset_id, path=f"media/{asset_id}.mp4",
                 generation_params={"prompt": asset_id, "workflow": {"seed": seed}})


def role_collect_paused_after_read(project_dir):
    real, paused = ps.validate_storage, []

    def validate(*args, **kwargs):
        result = real(*args, **kwargs)
        if not paused:
            paused.append(True)
            raise_flag("collector_read")
            wait_flag("saved")
        return result

    ps.validate_storage = validate
    report(collect(project_dir, retention_seconds=0))


def role_collect_after_publication(project_dir):
    wait_flag("published")
    report(collect(project_dir, retention_seconds=0))
    raise_flag("collected")


def role_save_after_collector_read(project_dir, seed):
    project = pm.load_project(project_dir)
    project.assets.append(generated("take", seed))
    wait_flag("collector_read")
    pm.save_project(project, notify=False)
    raise_flag("saved")


def role_save_paused_before_replace(project_dir, seed):
    project = pm.load_project(project_dir)
    project.assets.append(generated("take", seed))
    real = pm.atomic_replace

    def replace(source, target, **kwargs):
        if os.path.basename(target) == "project.json":
            raise_flag("published")
            wait_flag("collected")
        return real(source, target, **kwargs)

    pm.atomic_replace = replace
    pm.save_project(project, notify=False)


def role_stress(project_dir, tag, count):
    """Version-checked appends with the server's own retry, beside loads and sweeps."""
    acknowledged, conflicts = [], 0
    raise_flag(f"ready_{tag}")
    wait_flag("go")
    for index in range(int(count)):
        while True:
            project = pm.load_project(project_dir)
            project.assets.append(generated(f"{tag}{index}", f"{tag}{index}"))
            try:
                pm.save_project(project, expected_modified_at=project.modified_at, notify=False)
            except pm.ProjectVersionConflict:
                conflicts += 1
                continue
            break
        acknowledged.append(f"{tag}{index}")
        if index % 3 == 2:
            collect(project_dir)
    report({"acknowledged": acknowledged, "conflicts": conflicts})


def role_load_loop(project_dir):
    """Load and open every provenance component until told to stop."""
    loads = 0
    raise_flag("ready_loader")
    wait_flag("go")
    while not os.path.exists(os.path.join(FLAGS, "stop")):
        project = pm.load_project(project_dir)
        for asset in project.assets:
            assert asset.generation_params["prompt"] == asset.asset_id
        loads += 1
    report({"loads": loads})


def role_hold(project_dir):
    with pm._project_commit_lock(project_dir) as held:
        assert held
        # The OS lock itself, not only an exclusive hold: a second handle is refused.
        assert file_lock.ProjectFileLock(project_dir).try_acquire() == file_lock.CONTENDED
        raise_flag("holding")
        wait_flag("release")


def role_save(project_dir, seed):
    project = pm.load_project(project_dir)
    project.assets.append(generated("other", seed))
    pm.save_project(project, notify=False)
    raise_flag("other_saved")


def role_supersede(project_dir):
    """Replace asset a0's provenance until nothing retains the first one, then sweep."""
    project = pm.load_project(project_dir)
    project.assets[0].generation_params = {"prompt": "superseded"}
    for _ in range(3):
        pm.save_project(project, notify=False)
    report(collect(project_dir))


def role_reader_paused_before_children(project_dir):
    """Read the root, pause, then open what it names."""
    real, paused = ps.validate_storage, []

    def validate(*args, **kwargs):
        if not paused:
            paused.append(True)
            raise_flag("root_read")
            wait_flag("replaced")
        return real(*args, **kwargs)

    ps.validate_storage = validate
    project = pm.load_project(project_dir)
    report({"prompts": [asset.generation_params["prompt"] for asset in project.assets],
            "history": ps.read_prompt_history(project)})


if __name__ == "__main__":
    # This process has no ComfyUI user directory, so it has no server settings file: the
    # test says whether it shares its projects, and it must, or no role takes the lock.
    if os.environ.get("CROSS_PROCESS_SHARING") == "1":
        server_settings.sharing_enabled = lambda: True
    assert server_settings.sharing_enabled(), "this worker would not take the cross-process lock"
    globals()["role_" + sys.argv[2]](*sys.argv[3:])
