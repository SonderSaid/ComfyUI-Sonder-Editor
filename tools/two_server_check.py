"""Two real ComfyUI servers saving one project: does it stay readable and whole?

    python tools/two_server_check.py --comfy <ComfyUI folder> [--pack <checkout>]
        [--seconds 150] [--gap 1.0] [--one-server] [--bare]

Development only. Starts both servers on CPU with this pack alone and their own output,
user, temp and input folders under a temporary directory, so no existing project is
opened. Run it with any Python that can import the pack's server package; the servers
use the ComfyUI folder's venv. Exits non-zero when a check fails.

Both servers enqueue jobs (version-checked writes) and load the project throughout.
``--bare`` adds favorite writes, which have no version check, and only reports their
losses: one server loses those too (run ``--one-server`` to compare).
"""
import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parents[1]
PORTS = (8911, 8912)
PACK_NAME = "sonder-editor-check"


def parse():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--comfy", required=True, type=Path)
    parser.add_argument("--pack", type=Path, default=HERE)
    parser.add_argument("--seconds", type=float, default=150)
    parser.add_argument("--gap", type=float, default=1.0, help="mean seconds between one client's writes")
    parser.add_argument("--assets", type=int, default=300)
    parser.add_argument("--one-server", action="store_true")
    parser.add_argument("--bare", action="store_true")
    return parser.parse_args()


def make_project(base, assets):
    sys.path.insert(0, str(HERE))
    from server import project_manager as pm
    from server.timeline_state import Asset, Scene
    project = pm.create_project("ZZ-two-servers-" + uuid.uuid4().hex[:6], base_dir=str(base))
    project.assets = [Asset(asset_id=f"a{i}", name=f"a{i}", path=f"media/a{i}.mp4",
                            generation_params={"prompt": f"p{i} " * 50, "workflow": {"n": i, "pad": "x" * 1500}})
                      for i in range(assets)]
    if not project.scenes:
        project.scenes = [Scene(scene_id="s1", name="Scene 1")]
    scene = project.scenes[0].scene_id
    pm.save_project(project, notify=False)
    return Path(project.project_dir), scene


class Servers:
    def __init__(self, args, root):
        self.output = root / "output"
        nodes = root / "custom_nodes"
        nodes.mkdir(parents=True)
        link = nodes / PACK_NAME
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(args.pack.resolve())],
                           check=True, stdout=subprocess.DEVNULL)
        else:
            link.symlink_to(args.pack.resolve())
        self.link = link
        self.processes, self.logs = {}, {}

    def start(self, args, root):
        config = root / "paths.yaml"
        config.write_text(f"check:\n  base_path: {root.as_posix()}\n  custom_nodes: custom_nodes\n", encoding="utf-8")
        python = args.comfy / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        for port in PORTS[:1 if args.one_server else 2]:
            folders = {kind: root / f"{kind}{port}" for kind in ("user", "temp", "input")}
            for folder in folders.values():
                folder.mkdir()
            self.logs[port] = root / f"server{port}.log"
            self.processes[port] = subprocess.Popen(
                [str(python), "main.py", "--port", str(port), "--cpu", "--disable-auto-launch", "--offline",
                 "--output-directory", str(self.output), "--user-directory", str(folders["user"]),
                 "--temp-directory", str(folders["temp"]), "--input-directory", str(folders["input"]),
                 "--database-url", "sqlite:///:memory:", "--extra-model-paths-config", str(config),
                 "--disable-all-custom-nodes", "--whitelist-custom-nodes", PACK_NAME],
                cwd=args.comfy, env=env, stdout=self.logs[port].open("w"), stderr=subprocess.STDOUT)

    def stop(self):
        for process in self.processes.values():
            process.terminate()
        for process in self.processes.values():
            try:
                process.wait(30)
            except subprocess.TimeoutExpired:
                process.kill()
        if os.name == "nt":
            os.rmdir(self.link)  # the junction only, never what it points at
        else:
            self.link.unlink()


def request(port, project, method, path, body=None):
    origin = f"http://127.0.0.1:{port}"
    call = urllib.request.Request(
        f"{origin}/sonder-editor/project/{project}{path}", method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Origin": origin})
    try:
        with urllib.request.urlopen(call, timeout=120) as answer:
            return answer.status, json.loads(answer.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, error.read()[:200].decode("utf-8", "replace")
    except (OSError, ValueError) as error:
        return 0, repr(error)[:160]


def broken_components(project_dir, document):
    """Every current and retained component, history entries included, that is not intact."""
    broken = []

    def read(name, descriptor):
        try:
            payload = (project_dir / descriptor["path"]).read_bytes()
        except OSError:
            broken.append(name)
            return None
        if hashlib.sha256(payload).hexdigest() != descriptor["sha256"]:
            broken.append(name)
            return None
        return json.loads(payload)

    for group in ("components", "recovery_previous", "authoring_previous"):
        for name, descriptor in document["storage"].get(group, {}).items():
            value = read(name, descriptor)
            if name == "prompt_history" and isinstance(value, dict):
                for entry in value["entries"]:
                    read("history_entry", entry)
    return broken


def run(args, root):
    project_dir, scene = make_project(root / "output" / "sonder-projects", args.assets)
    project = project_dir.name
    servers = Servers(args, root)
    failures = []
    try:
        servers.start(args, root)
        for port in servers.processes:
            deadline = time.time() + 240
            while request(port, project, "GET", "/queue")[0] != 200:
                if time.time() > deadline or servers.processes[port].poll() is not None:
                    raise SystemExit(f"server {port} did not start: {servers.logs[port].read_text()[-800:]}")
                time.sleep(1)
        ports = list(servers.processes)
        end = time.time() + args.seconds
        statuses, acknowledged, favorites = {}, {port: [] for port in PORTS}, []
        latencies, first_bad = [], []
        lock = threading.Lock()

        def note(kind, port, code, body, allowed):
            with lock:
                key = f"{kind}@{port}"
                statuses.setdefault(key, {}).setdefault(code, 0)
                statuses[key][code] += 1
                if code not in allowed and not first_bad:
                    first_bad.append((round(time.time() - end + args.seconds, 1), key, code, body))

        def pause():
            time.sleep(random.uniform(0.4, 1.6) * args.gap)

        def writer(slot):
            port = ports[slot % len(ports)]
            while time.time() < end:
                started = time.perf_counter()
                code, body = request(port, project, "POST", "/queue", {
                    "scene_id": scene, "prompt": uuid.uuid4().hex,
                    "prompt_sections": [{"start_frame": 0, "end_frame": 24, "prompt": uuid.uuid4().hex}]})
                note(f"enqueue{slot}", port, code, body, {200, 409})
                if code == 200:
                    latencies.append(time.perf_counter() - started)
                    acknowledged[PORTS[slot]].append(body["job_id"])
                pause()

        def loader(slot):
            port = ports[slot % len(ports)]
            while time.time() < end:
                note(f"load{slot}", port, *request(port, project, "GET", ""), {200})
                ids = "&".join(f"asset_id=a{random.randrange(args.assets)}" for _ in range(8))
                note(f"details{slot}", port, *request(port, project, "GET", f"/assets/provenance?{ids}"), {200})
                pause()

        def favorite():
            index = 0
            while time.time() < end:
                code, body = request(ports[-1], project, "PUT", f"/assets/a{index % args.assets}", {"favorite": True})
                note("favorite", ports[-1], code, body, {200, 409})
                if code == 200:
                    favorites.append(f"a{index % args.assets}")
                index += 1
                pause()

        def guarded(target, *target_args):
            try:
                target(*target_args)
            except Exception as error:  # noqa: BLE001 - a client that died is a failed check
                with lock:
                    failures.append(f"a client stopped: {error!r}")

        work = [threading.Thread(target=guarded, args=(target, slot))
                for slot in (0, 1) for target in (writer, loader)]
        if args.bare:
            work.append(threading.Thread(target=guarded, args=(favorite,)))
        for thread in work:
            thread.start()
        for thread in work:
            thread.join()

        document = json.loads((project_dir / "project.json").read_bytes())
        present = {job["job_id"] for job in document["generation_queue"]}
        lost = {port: len([job for job in jobs if job not in present]) for port, jobs in acknowledged.items()}
        broken = broken_components(project_dir, document)
        after = {port: request(port, project, "GET", "")[0] for port in ports}
        exits = {port: process.poll() for port, process in servers.processes.items()}
        logged = sum(servers.logs[port].read_text(errors="replace").count("maintenance failed") for port in ports)

        print("statuses", json.dumps(statuses))
        print("acknowledged jobs", {port: len(jobs) for port, jobs in acknowledged.items()}, "lost", lost)
        if latencies:
            ordered = sorted(latencies)
            print(f"enqueue seconds: median {ordered[len(ordered) // 2]:.3f}, max {ordered[-1]:.3f}")
        if args.bare:
            stored = {asset["asset_id"] for asset in document["assets"] if asset.get("favorite")}
            print("favorites (unguarded, reported only)", len(set(favorites)), "lost", len(set(favorites) - stored))
        print("components not intact", len(broken), "| load after", after, "| server exits", exits,
              "| maintenance failures", logged, "| first unexpected", first_bad)

        minimum = max(5, int(args.seconds / args.gap / 8))
        if any(len(acknowledged[port]) < minimum for port in PORTS):
            failures.append(f"fewer than {minimum} acknowledged writes on a client")
        for kind in ("load", "details"):
            for slot in (0, 1):
                key = f"{kind}{slot}@{ports[slot % len(ports)]}"
                if statuses.get(key, {}).get(200, 0) < minimum:
                    failures.append(f"fewer than {minimum} reads by {key}")
        if broken:
            failures.append(f"{len(broken)} components missing or damaged")
        if set(after.values()) != {200}:
            failures.append("the project no longer loads")
        if any(code is not None for code in exits.values()):
            failures.append("a server exited")
        if first_bad:
            failures.append(f"unexpected response {first_bad[0]}")
        if logged:
            failures.append("storage maintenance failed")
        if not args.bare and any(lost.values()):
            failures.append(f"acknowledged version-checked writes lost: {lost}")
    finally:
        servers.stop()
    return failures


def main():
    args = parse()
    root = Path(tempfile.mkdtemp(prefix="sonder-two-servers-"))
    try:
        failures = run(args, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("FAILED: " + "; ".join(failures) if failures else "PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
