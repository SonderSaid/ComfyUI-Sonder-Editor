"""Project I/O runs on its own pool, so a migrating save cannot starve everything else.

`asyncio.to_thread` uses the event loop's default executor, shared with ffmpeg
thumbnailing, ffprobe, media streaming and per-chunk upload disk checks. A migrating
save holds the per-project write lock for seconds and every project route waiting on
it parks a default-pool thread doing nothing — the loop stays responsive while the
pool does not.
"""
import asyncio
import threading

import pytest

from server import project_storage_lifecycle as lifecycle


@pytest.fixture(autouse=True)
def _fresh_project_pool():
    """Re-arm before, shut down after, so no test leaks a live pool into the suite."""
    lifecycle.start_project_executor()
    yield
    lifecycle._shutdown_project_executor()
    lifecycle.start_project_executor()


def test_project_io_runs_on_the_project_pool_not_the_default_one():
    async def run():
        loop_thread = threading.get_ident()
        names = []

        def record():
            names.append(threading.current_thread().name)
            return threading.get_ident()

        worker_thread = await lifecycle.run_project_io(record)

        assert worker_thread != loop_thread, "project I/O ran on the event loop"
        assert names and names[0].startswith(lifecycle.PROJECT_EXECUTOR_THREAD_PREFIX), (
            f"project I/O ran on {names[0]!r}, not the project pool")

    asyncio.run(run())


def test_a_parked_project_pool_does_not_delay_unrelated_work():
    """The measured failure: unrelated `to_thread` work queued behind project waits."""
    async def run():
        release = threading.Event()
        parked_count = lifecycle._project_executor_size()
        # A barrier, not a single Event: an Event set by the FIRST worker proves one
        # task started, which is not the precondition this test needs. The barrier
        # trips only once every pool thread is occupied.
        saturated = threading.Barrier(parked_count + 1)

        def park():
            saturated.wait(10)
            assert release.wait(10), "parked project work was never released"

        # Fill the project pool completely, the way routes waiting on one project's
        # write lock would during an automatic component migration.
        jobs = [asyncio.create_task(lifecycle.run_project_io(park))
                for _ in range(parked_count)]
        await asyncio.to_thread(saturated.wait, 10)

        try:
            # Unrelated default-pool work — a thumbnail, a probe, an upload's disk
            # check — must still complete while every project thread is parked.
            unrelated = await asyncio.wait_for(
                asyncio.to_thread(lambda: "done"), timeout=5)
            assert unrelated == "done"
        finally:
            release.set()
            await asyncio.gather(*jobs)

    asyncio.run(run())


def test_project_pool_work_may_not_resubmit_to_the_project_pool():
    """A bounded pool waiting on itself deadlocks; refuse rather than discover it."""
    async def run():
        def on_pool_thread():
            assert lifecycle.on_project_executor_thread()
            # Reaching `run_project_io` from here is the deadlock shape.
            loop = asyncio.new_event_loop()
            try:
                with pytest.raises(RuntimeError, match="must not resubmit"):
                    loop.run_until_complete(lifecycle.run_project_io(lambda: None))
            finally:
                loop.close()
            return "refused"

        assert await lifecycle.run_project_io(on_pool_thread) == "refused"

    asyncio.run(run())


def test_a_shut_down_pool_refuses_work_instead_of_resurrecting():
    """`on_cleanup` and the atexit fallback can both fire; neither may raise.

    Silent resurrection was the original defect: a request arriving after the global
    was nulled built a SECOND pool that cleanup had already walked past and would
    never join, so `on_cleanup` returned with live unowned threads. Refusing is the
    honest answer — the server is stopping — and re-arming is explicit.
    """
    async def submit():
        return await lifecycle.run_project_io(lambda: "ran")

    assert asyncio.run(submit()) == "ran"

    lifecycle._shutdown_project_executor()
    lifecycle._shutdown_project_executor()  # both hooks may fire; must not raise

    with pytest.raises(lifecycle.ProjectExecutorStopped):
        asyncio.run(submit())
    assert not [t for t in threading.enumerate()
                if t.name.startswith(lifecycle.PROJECT_EXECUTOR_THREAD_PREFIX)], (
        "shutdown returned with live pool threads")

    lifecycle.start_project_executor()
    assert asyncio.run(submit()) == "ran", "an explicit re-arm must rebuild the pool"


def test_the_pool_is_not_smaller_than_the_loop_default_executor():
    """Isolation is the goal, not a different degree of parallelism.

    Asserting the property rather than restating the formula: a smaller bound would
    make one project's migrating save starve OTHER projects' reads worse than sharing
    the default pool does, because threads parked on project A's lock still block
    project B. Re-deriving `min(32, cpu+4)` here would only detect an edit, not a
    regression.
    """
    import asyncio
    import concurrent.futures

    async def default_executor_size():
        loop = asyncio.get_running_loop()
        await asyncio.to_thread(lambda: None)  # force the default executor into being
        default = loop._default_executor
        assert isinstance(default, concurrent.futures.ThreadPoolExecutor)
        return default._max_workers

    default_size = asyncio.run(default_executor_size())
    assert lifecycle._project_executor_size() >= default_size, (
        f"project pool is {lifecycle._project_executor_size()} threads against a "
        f"{default_size}-thread default executor; a smaller project pool makes "
        "cross-project starvation worse than sharing does")


def test_a_pool_task_can_name_the_frame_that_submitted_it():
    """The submitter reaches the worker, and it is the awaiting frame.

    Every real call site is a direct `await` in a route handler body, so frame 1 of
    `run_project_io` is that handler. Asserting the function name pins "the awaiting
    frame" rather than something further up the stack, which is the property the
    attribution depends on.
    """
    async def stands_in_for_a_route_handler():
        return await lifecycle.run_project_io(lifecycle.project_io_caller)

    submitted = asyncio.run(stands_in_for_a_route_handler())

    assert submitted is not None, "the pool task could not name its submitter"
    filename, lineno, name = submitted
    assert name == "stands_in_for_a_route_handler", (
        f"expected the awaiting frame, got {name!r}")
    assert filename.endswith("test_project_executor.py")
    assert isinstance(lineno, int) and lineno > 0


def test_a_pooled_save_records_both_the_pool_frame_and_the_submitting_route(monkeypatch):
    """The two fields asserted where they are actually emitted.

    The test above proves the submitter reaches the worker; this one proves
    `save_project` reads it and puts it on the event. That emitting block is wrapped in
    `except Exception`, because a diagnostic must never fail a save — which also means a
    rename or an arity change there stops emitting the field silently, with the suite
    still green. This is the assertion that would notice.
    """
    import tempfile
    from server import project_manager, session_registry

    events = []
    monkeypatch.setattr(
        session_registry, "record_diag_event",
        lambda kind, project_id="", host_id="", **details: events.append((kind, details)))

    with tempfile.TemporaryDirectory() as base_dir:
        project = project_manager.create_project("Pooled Save", base_dir=base_dir)
        events.clear()

        async def a_route_handler():
            return await lifecycle.run_project_io(project_manager.save_project, project)

        asyncio.run(a_route_handler())

    saved = [details for kind, details in events if kind == "project_saved"]
    assert len(saved) == 1, f"expected one project_saved event, got {len(saved)}"
    details = saved[0]

    assert "submitted_by" in details, (
        "a pooled save did not record the route that submitted it — and the emitting "
        "block swallows its own exceptions, so nothing else would have told us")
    assert details["submitted_by"].startswith("test_project_executor.py:")
    assert details["submitted_by"].endswith(" a_route_handler")
    # `caller` still means the immediate frame, unchanged. Here `save_project` IS the
    # submitted callable, so that frame is the pool's own runner — which is true, and is
    # the reason `submitted_by` was added beside it rather than replacing it.
    assert "thread.py:" in details["caller"], (
        f"expected the pool runner as the immediate frame, got {details['caller']!r}")


def test_a_submitter_does_not_leak_into_the_next_task_on_a_reused_thread():
    """Pool threads outlive their tasks; a stale submitter is a confident wrong answer.

    Worse than no attribution, because it names a real route that did not do the work.
    Run enough sequential tasks that reuse is certain, and read the submitter from a
    task that was submitted with none.
    """
    used = set()

    async def attributed_work():
        for _ in range(8):
            await lifecycle.run_project_io(
                lambda: used.add(threading.current_thread().name))

    asyncio.run(attributed_work())

    # Raw submits, deliberately bypassing `_submit_project_io` and its context copy.
    # This is work reaching a pool thread without anything setting the variable, so it
    # reads whatever that thread was left holding. `copy_context().run()` restores the
    # thread's own context when a task returns, so there is nothing to find; a
    # hand-cleared thread-local that ever missed its reset would surface right here.
    #
    # Reuse is the whole premise, and it is NOT guaranteed: `_adjust_thread_count` spawns
    # a fresh thread whenever its idle semaphore misses, and a fresh thread has a clean
    # context and returns None whether or not the mechanism leaks. So collect only
    # probes that landed on a thread which ran attributed work, and say so loudly if
    # none ever does, rather than passing on a sample that proves nothing.
    executor = lifecycle.project_executor()
    residue = []
    for _ in range(40):
        name, caller = executor.submit(
            lambda: (threading.current_thread().name, lifecycle.project_io_caller())
        ).result()
        if name in used:
            residue.append(caller)
        if len(residue) >= 3:
            break

    assert residue, (
        f"no probe ever landed on a reused thread (ran on {sorted(used)}), so this test "
        "exercised nothing — it is the reuse that makes a stale value observable")
    assert residue == [None] * len(residue), (
        f"a finished task's submitter was still set on its pool thread: {residue}")


def test_work_driven_by_asyncio_internals_records_nothing_rather_than_a_lie():
    """Under `create_task` the awaiting frame is asyncio, not the route.

    A frame inside `asyncio/events.py` reads like a plausible caller, so it is worse
    than an absent
    field — an investigation would chase the event loop instead of the handler. The
    route handler is not on that stack at all, so there is nothing truthful to record.
    """
    async def run():
        return await asyncio.create_task(
            lifecycle.run_project_io(lifecycle.project_io_caller))

    submitted = asyncio.run(run())
    assert submitted is None, (
        f"attributed a task-driven submit to an asyncio internal frame: {submitted}")


# ---------------------------------------------------------------------------
# The off-loop rewrite needs a guard of its own: without one, reverting any subset
# of it — or adding a handler that reaches for `asyncio.to_thread` — leaves the
# suite green. This scan found 20 missed sites the hand-written filter had let
# through, including two lambdas whose own comment said they took the project lock.
# ---------------------------------------------------------------------------

import ast

# Functions that acquire the per-project write lock, directly or through the lazy
# provenance hydration `Asset.__getattribute__` performs on `generation_params`.
LOCK_LEAVES = {
    "load_project", "save_project", "list_projects", "create_project",
    "hydrate_job", "hydrate_asset", "read_component", "read_prompt_history",
    "read_asset_provenance_batch", "read_asset_search_metadata_batch",
    "collect_unreferenced_components",
    "_project_write_lock", "generation_params",
}

# Deliberately left on the default pool: these are mostly ffmpeg, and running them on
# the project pool would occupy threads the project routes need — the same starvation
# pointed the other way. Their `load_project` reads can park a default-pool thread,
# which is the accepted cost. Expiry: remove an entry once its project resolution is
# hoisted out and it takes only paths.
DEFAULT_POOL_BY_DESIGN = {
    "_regenerate_thumbnail_if_current",
    "_generate_strip_if_current",
    "_generate_waveform_if_current",
}


def _lock_reaching_names(source: str) -> set:
    tree = ast.parse(source)

    def referenced(node):
        names = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                names.add(getattr(child.func, "id", None)
                          or getattr(child.func, "attr", None))
            elif isinstance(child, ast.Attribute):
                names.add(child.attr)
        return {name for name in names if name}

    bodies = {node.name: referenced(node) for node in ast.walk(tree)
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    reaching = set(LOCK_LEAVES)
    changed = True
    while changed:
        changed = False
        for name, called in bodies.items():
            if name not in reaching and called & reaching:
                reaching.add(name)
                changed = True
    return reaching


def _default_pool_lock_takers(source: str):
    """`asyncio.to_thread` sites whose callee can reach the project write lock."""
    tree = ast.parse(source)
    reaching = _lock_reaching_names(source)
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "to_thread"
                and getattr(func.value, "id", None) == "asyncio"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Lambda):
            names = {getattr(n.func, "id", None) for n in ast.walk(first)
                     if isinstance(n, ast.Call)}
            hit = sorted(name for name in names if name in reaching)
            label = f"lambda calling {', '.join(hit)}" if hit else None
        else:
            name = getattr(first, "id", None) or getattr(first, "attr", None)
            label = name if name in reaching and name not in DEFAULT_POOL_BY_DESIGN else None
        if label:
            findings.append((node.lineno, label))
    return sorted(findings)


def test_project_lock_work_is_submitted_to_the_project_pool(monkeypatch):
    from test_project_mutation_pipeline_backend import _load_route_module

    routes = _load_route_module(monkeypatch)
    with open(routes.__file__, encoding="utf-8") as handle:
        findings = _default_pool_lock_takers(handle.read())

    assert findings == [], (
        "these reach the per-project write lock but were submitted to the shared "
        "default pool, where they park threads that ffmpeg, ffprobe and uploads need: "
        + "; ".join(f"routes.py:{line} {label}" for line, label in findings))


def test_the_pool_scan_would_catch_a_regression():
    """Guards the guard, including the lambda form the first hand-written filter missed."""
    reverted = (
        "async def handler(request):\n"
        "    return await asyncio.to_thread(load_project, d)\n"
    )
    assert _default_pool_lock_takers(reverted) == [(2, "load_project")]

    # Includes the reachability chain: `_asset_payload` is not a lock leaf, it
    # REACHES one through the lazy hydration on `generation_params`. That indirection
    # is exactly what hid these two lambdas from the first hand-written filter.
    lambda_form = (
        "def _asset_payload(p, a):\n"
        "    return a.generation_params\n"
        "async def handler(request):\n"
        "    return await asyncio.to_thread(lambda: _asset_payload(p, a))\n"
    )
    assert _default_pool_lock_takers(lambda_form) == [
        (4, "lambda calling _asset_payload")]

    indirect = (
        "def _wrapper(d):\n"
        "    return load_project(d)\n"
        "async def handler(request):\n"
        "    return await asyncio.to_thread(_wrapper, d)\n"
    )
    assert _default_pool_lock_takers(indirect) == [(4, "_wrapper")], (
        "the scan must follow one hop, or a thin wrapper hides the lock")

    pooled = (
        "async def handler(request):\n"
        "    return await run_project_io(load_project, d)\n"
    )
    assert _default_pool_lock_takers(pooled) == []
