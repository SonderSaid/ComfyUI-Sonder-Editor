/**
 * Bounded prompt-preview request lanes.
 *
 * Each purpose/project/scene key owns one active request and, while it runs,
 * only the newest trailing intent. Scheduling a newer intent immediately
 * revokes the active intent's result and retry authority. The physical request
 * may still finish because the deployed aiohttp server does not cancel handler
 * work on browser disconnect.
 */
export function createPromptCompileCoordinator(dispatch) {
    if (typeof dispatch !== "function") {
        throw new TypeError("Prompt compile coordinator requires a dispatch function");
    }
    const lanes = new Map();
    let disposed = false;
    let sequence = 0;

    const laneKey = ({ purpose, projectId, sceneId }) => JSON.stringify([
        String(purpose || ""), String(projectId || ""), String(sceneId || ""),
    ]);

    const owns = (lane, intent) => !disposed && intent.current
        && lane.active === intent && intent.isCurrent();

    const retireLaneIfIdle = (key, lane) => {
        if (!lane.active && !lane.trailing && lanes.get(key) === lane) lanes.delete(key);
    };

    const start = (key, lane, intent) => {
        lane.active = intent;
        Promise.resolve().then(() => dispatch({
            purpose: intent.purpose,
            projectId: intent.projectId,
            sceneId: intent.sceneId,
            body: structuredClone(intent.body),
            requestId: intent.requestId,
            isCurrent: () => owns(lane, intent),
        })).then((value) => {
            intent.resolve(owns(lane, intent) ? value : null);
        }, (error) => {
            if (owns(lane, intent)) intent.reject(error);
            else intent.resolve(null);
        }).finally(() => {
            if (lane.active === intent) lane.active = null;
            const trailing = lane.trailing;
            lane.trailing = null;
            if (!disposed && trailing?.current) start(key, lane, trailing);
            else if (trailing) trailing.resolve(null);
            retireLaneIfIdle(key, lane);
        });
    };

    const schedule = ({ purpose, projectId, sceneId, body,
        isCurrent = () => true } = {}) => new Promise((resolve, reject) => {
        if (disposed) { resolve(null); return; }
        const key = laneKey({ purpose, projectId, sceneId });
        let lane = lanes.get(key);
        if (!lane) {
            lane = { active: null, trailing: null };
            lanes.set(key, lane);
        }
        const intent = {
            purpose: String(purpose || ""),
            projectId: String(projectId || ""),
            sceneId: String(sceneId || ""),
            body: structuredClone(body),
            isCurrent,
            requestId: `prompt-${++sequence}`,
            current: true,
            resolve,
            reject,
        };
        if (!lane.active) {
            start(key, lane, intent);
            return;
        }
        lane.active.current = false;
        if (lane.trailing) {
            lane.trailing.current = false;
            lane.trailing.resolve(null);
        }
        lane.trailing = intent;
    });

    const dispose = () => {
        if (disposed) return;
        disposed = true;
        for (const [key, lane] of lanes) {
            if (lane.active) lane.active.current = false;
            if (lane.trailing) {
                lane.trailing.current = false;
                lane.trailing.resolve(null);
                lane.trailing = null;
            }
            retireLaneIfIdle(key, lane);
        }
    };

    const debugState = () => ({
        disposed,
        lanes: [...lanes.entries()].map(([key, lane]) => ({
            key,
            active: lane.active?.requestId || null,
            trailing: lane.trailing?.requestId || null,
        })),
    });

    return { schedule, dispose, debugState };
}
