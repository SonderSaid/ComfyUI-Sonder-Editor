export class ProjectMutationQueue {
    constructor({ onIdle = null } = {}) {
        this._pending = [];
        this._active = null;
        this._drainWaiters = [];
        this._pumpScheduled = false;
        this._onIdle = typeof onIdle === "function" ? onIdle : null;
    }

    enqueue({ key, label = "", coalesce = true, merge = null, intent = null, run }) {
        if (!key) {
            return Promise.reject(new Error("Project mutation key is required"));
        }
        if (typeof run !== "function") {
            return Promise.reject(new Error("Project mutation run function is required"));
        }

        return new Promise((resolve, reject) => {
            const waiter = { resolve, reject };
            const existing = coalesce !== false
                ? this._pending.find((mutation) => mutation.key === key)
                : null;

            if (existing) {
                if (typeof merge === "function") {
                    existing.intent = merge(existing.intent, intent);
                } else {
                    existing.intent = intent;
                }
                existing.label = label || existing.label;
                existing.run = run;
                existing.waiters.push(waiter);
            } else {
                this._pending.push({
                    key,
                    label,
                    intent,
                    run,
                    waiters: [waiter],
                });
            }
            this._schedulePump();
        });
    }

    hasPending() {
        return this._pending.length > 0;
    }

    isActive() {
        return !!this._active;
    }

    isBusy() {
        return this.isActive() || this.hasPending();
    }

    drain(_reason = "drain") {
        if (!this.isBusy()) return Promise.resolve();
        return new Promise((resolve) => {
            this._drainWaiters.push(resolve);
            this._schedulePump();
        });
    }

    _schedulePump() {
        if (this._pumpScheduled) return;
        this._pumpScheduled = true;
        queueMicrotask(() => {
            this._pumpScheduled = false;
            void this._pump();
        });
    }

    async _pump() {
        if (this._active) return;
        while (this._pending.length > 0) {
            const mutation = this._pending.shift();
            this._active = mutation;
            try {
                const result = await mutation.run(mutation.intent);
                for (const waiter of mutation.waiters) {
                    waiter.resolve(result);
                }
            } catch (error) {
                for (const waiter of mutation.waiters) {
                    waiter.reject(error);
                }
            } finally {
                this._active = null;
            }
        }
        this._flushDrainWaiters();
    }

    _flushDrainWaiters() {
        if (this.isBusy()) return;
        const waiters = this._drainWaiters.splice(0);
        for (const resolve of waiters) {
            resolve();
        }
        this._onIdle?.();
    }
}
