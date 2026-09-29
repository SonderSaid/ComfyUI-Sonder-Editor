/**
 * Bounded paired prompt-preview requests (`plans/cut-read-fanout.md` §3).
 *
 * One lane per project/scene owns ONE active physical request and, while it
 * runs, the latest trailing intent for each branch. A request carries up to
 * two branches -- "windowed" and "scene" -- and each branch keeps its own
 * semantic key, its own application guard (`isCurrent`) and its own result
 * promise, so one branch can be superseded, fail or be satisfied without the
 * other noticing.
 *
 * A branch spec is `{ key, isCurrent(), stillNeeded(), answers(body),
 * acceptsCarried(result) }`:
 *
 *   - `isCurrent` is the host's authority; a branch whose guard has lapsed
 *     resolves `null` and never applies.
 *   - `stillNeeded` is asked immediately before a branch is sent. A result that
 *     landed meanwhile may already answer it; the branch then resolves
 *     `PROMPT_BRANCH_SATISFIED` and costs nothing.
 *   - `answers(body)` says whether a request built from `body` produces this
 *     branch's result. One physical request carries one candidate, so a branch
 *     rides with another intent's body only when it answers for it.
 *   - `acceptsCarried(result)` is asked of an active branch that a newer
 *     same-key intent CARRIED rather than revoked: its request was sent at an
 *     older version, so its success lands only when the host can prove that
 *     version's result still holds. Anything else is left to the re-request.
 *
 * Settlement is per branch: a record settles its branch; a missing record, a
 * malformed stream or a broken connection fails only the branches still
 * unsettled. A superseded request is NOT aborted: the server cannot cancel a
 * compile already running, so freeing the lane early would only let the next
 * request run beside it. The lane stays occupied until the response ends, which
 * bounds the server to one compile per lane. Only `dispose` aborts, when
 * nothing will ever read the answer; the server then skips the projections it
 * has not started.
 */
export const PROMPT_PREVIEW_BRANCH_NAMES = Object.freeze(["windowed", "scene"]);
export const PROMPT_BRANCH_SATISFIED = Object.freeze({ satisfied: true });

export function createPromptCompileCoordinator(dispatch) {
    if (typeof dispatch !== "function") {
        throw new TypeError("Prompt compile coordinator requires a dispatch function");
    }
    const lanes = new Map();
    let disposed = false;
    let sequence = 0;

    const laneKey = (projectId, sceneId) => JSON.stringify([
        String(projectId || ""), String(sceneId || ""),
    ]);

    const live = (entry) => !disposed && entry.current && !entry.settled
        && entry.spec.isCurrent?.() !== false;

    const settle = (entry, value) => {
        if (entry.settled) return;
        entry.settled = true;
        entry.resolve(value);
    };

    // A branch carried under a queued re-request for the same result lands
    // only a success: its failure is not the last word, because the re-request
    // is sent unless that success arrives first.
    const fail = (entry, error) => {
        if (entry.settled) return;
        const applies = live(entry) && !entry.successOnly;
        entry.settled = true;
        if (applies) entry.reject(error);
        else entry.resolve(null);
    };

    const deliver = (entry, result) => {
        if (entry.settled) return;
        const applies = live(entry) && !(entry.successOnly
            && (result?.response?.ok !== true
                || entry.spec.acceptsCarried?.(result) === false));
        entry.settled = true;
        entry.resolve(applies ? result : null);
    };

    const unsettled = (physical) => Object.values(physical.branches)
        .filter((entry) => !entry.settled);

    // Abort an unfinished request on dispose. Aborting settles nothing by
    // itself: dispatch rejects, and each branch then resolves null.
    const abortActive = (lane) => {
        const physical = lane.active;
        if (!physical || physical.aborted || !unsettled(physical).length) return;
        physical.aborted = true;
        physical.controller?.abort();
    };

    const retireLaneIfIdle = (key, lane) => {
        if (!lane.active && !lane.trailing.length && lanes.get(key) === lane) lanes.delete(key);
    };

    const start = (key, lane, intent) => {
        const send = {};
        for (const name of PROMPT_PREVIEW_BRANCH_NAMES) {
            const entry = intent.branches[name];
            if (!entry || entry.settled) continue;
            if (!live(entry)) { settle(entry, null); continue; }
            // Recalculated here, not when the intent was made: a result that
            // landed while this waited may already answer it.
            if (entry.spec.stillNeeded?.() === false) {
                settle(entry, PROMPT_BRANCH_SATISFIED);
                continue;
            }
            send[name] = entry;
        }
        const projections = Object.keys(send);
        if (!projections.length) return false;
        const controller = typeof AbortController === "function" ? new AbortController() : null;
        const physical = { branches: send, controller, aborted: false,
            requestId: `prompt-${++sequence}` };
        lane.active = physical;
        Promise.resolve().then(() => dispatch({
            projectId: intent.projectId,
            sceneId: intent.sceneId,
            body: structuredClone(intent.body),
            projections,
            requestId: physical.requestId,
            signal: controller?.signal,
            isCurrent: () => unsettled(physical).some(live),
            onRecord: (name, result) => {
                const entry = send[name];
                if (entry) deliver(entry, result);
            },
        })).then(() => {
            for (const entry of unsettled(physical)) {
                fail(entry, new Error("The prompt preview ended without a result."));
            }
        }, (error) => {
            for (const entry of unsettled(physical)) fail(entry, error);
        }).finally(() => {
            if (lane.active === physical) lane.active = null;
            startNext(key, lane);
        });
        return true;
    };

    const startNext = (key, lane) => {
        while (!disposed && !lane.active && lane.trailing.length) {
            start(key, lane, lane.trailing.shift());
        }
        if (disposed) {
            for (const intent of lane.trailing) {
                for (const entry of Object.values(intent.branches)) settle(entry, null);
            }
            lane.trailing = [];
        }
        retireLaneIfIdle(key, lane);
    };

    /**
     * Queue behind the active request. A newer branch supersedes the queued
     * one of the same name; a queued branch of the other name joins this
     * intent's body when it answers for it, and otherwise waits as its own
     * request. So at most one queued request per branch ever exists.
     */
    const enqueue = (lane, intent) => {
        const names = Object.keys(intent.branches);
        for (const queued of lane.trailing) {
            for (const name of names) {
                const entry = queued.branches[name];
                if (!entry) continue;
                settle(entry, null);
                delete queued.branches[name];
            }
        }
        lane.trailing = lane.trailing.filter((queued) =>
            Object.values(queued.branches).some((entry) => !entry.settled));
        const last = lane.trailing[lane.trailing.length - 1];
        if (last && Object.values(last.branches).every((entry) =>
            entry.settled || entry.spec.answers?.(intent.body) === true)) {
            for (const [name, entry] of Object.entries(last.branches)) {
                if (!entry.settled) intent.branches[name] = entry;
            }
            lane.trailing.pop();
        }
        lane.trailing.push(intent);
    };

    const schedule = ({ projectId, sceneId, body, branches = {} } = {}) => {
        const results = {};
        const intent = {
            projectId: String(projectId || ""),
            sceneId: String(sceneId || ""),
            body: structuredClone(body),
            branches: {},
        };
        for (const name of PROMPT_PREVIEW_BRANCH_NAMES) {
            const spec = branches[name];
            if (!spec) continue;
            results[name] = new Promise((resolve, reject) => {
                intent.branches[name] = { name, spec, current: !disposed, settled: false,
                    resolve, reject };
            });
            if (disposed) settle(intent.branches[name], null);
        }
        if (disposed || !Object.keys(intent.branches).length) return results;
        const key = laneKey(projectId, sceneId);
        let lane = lanes.get(key);
        if (!lane) {
            lane = { active: null, trailing: [] };
            lanes.set(key, lane);
        }
        if (lane.active) {
            // A newer intent for a branch revokes the active one's authority,
            // unless the active one computes exactly what is now asked. Then
            // it is carried: its success may still land, and satisfies the
            // re-request; anything else is left to the re-request.
            for (const name of Object.keys(intent.branches)) {
                const active = lane.active.branches[name];
                if (!active || active.settled) continue;
                if (active.spec.answers?.(intent.body) === true) active.successOnly = true;
                else active.current = false;
            }
            enqueue(lane, intent);
        } else if (!start(key, lane, intent)) {
            retireLaneIfIdle(key, lane);
        }
        return results;
    };

    const dispose = () => {
        if (disposed) return;
        disposed = true;
        for (const [key, lane] of lanes) {
            if (lane.active) {
                for (const entry of Object.values(lane.active.branches)) entry.current = false;
                abortActive(lane);
            }
            for (const intent of lane.trailing) {
                for (const entry of Object.values(intent.branches)) settle(entry, null);
            }
            lane.trailing = [];
            retireLaneIfIdle(key, lane);
        }
    };

    const debugState = () => ({
        disposed,
        lanes: [...lanes.entries()].map(([key, lane]) => ({
            key,
            active: lane.active ? {
                requestId: lane.active.requestId,
                branches: Object.keys(lane.active.branches),
                aborted: lane.active.aborted,
            } : null,
            trailing: lane.trailing.map((intent) => Object.keys(intent.branches)),
        })),
    });

    return { schedule, dispose, debugState };
}
