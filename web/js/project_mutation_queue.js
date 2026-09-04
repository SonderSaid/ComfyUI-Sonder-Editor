export class ProjectMutationQueue {
    constructor({ onIdle = null } = {}) {
        this._pending = [];
        this._active = null;
        this._activeOwnerToken = null;
        this._coalescingEpoch = 0;
        this._drainWaiters = [];
        this._drainFlushScheduled = false;
        this._pumpScheduled = false;
        this._onIdle = typeof onIdle === "function" ? onIdle : null;
    }

    enqueue({ key, label = "", coalesce = true, merge = null, intent = null,
        diagnostics = null, ownerToken = null, sealCoalescing = false, run }) {
        if (!key) {
            return Promise.reject(new Error("Project mutation key is required"));
        }
        if (typeof run !== "function") {
            return Promise.reject(new Error("Project mutation run function is required"));
        }

        // A nested write owned by the currently running item executes inline.
        // Inline work deliberately does not replace `_active` or appear in
        // `_pending`; isActive() stays true and hasPendingKey() sees only real
        // queue slots. Missing, foreign, and stale tokens fail closed by taking
        // the ordinary queue path.
        if (ownerToken && this._active && ownerToken === this._activeOwnerToken) {
            if (diagnostics && typeof diagnostics === "object") {
                diagnostics.coalescedCount = 1;
            }
            return Promise.resolve().then(() => run(intent, diagnostics, ownerToken));
        }

        if (sealCoalescing) this._coalescingEpoch += 1;
        const coalescingEpoch = this._coalescingEpoch;

        return new Promise((resolve, reject) => {
            const waiter = { resolve, reject };
            const existing = coalesce !== false
                ? this._pending.find((mutation) => mutation.key === key
                    && mutation.coalescingEpoch === coalescingEpoch)
                : null;

            if (existing) {
                if (typeof merge === "function") {
                    existing.intent = merge(existing.intent, intent);
                } else {
                    existing.intent = intent;
                }
                existing.label = label || existing.label;
                existing.coalescedCount += 1;
                existing.diagnostics = diagnostics;
                if (diagnostics && typeof diagnostics === "object") {
                    diagnostics.coalescedCount = existing.coalescedCount;
                }
                existing.run = run;
                existing.waiters.push(waiter);
            } else {
                if (diagnostics && typeof diagnostics === "object") {
                    diagnostics.coalescedCount = 1;
                }
                this._pending.push({
                    key,
                    label,
                    intent,
                    diagnostics,
                    coalescingEpoch,
                    coalescedCount: 1,
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

    hasPendingKey(key, { currentEpochOnly = false } = {}) {
        return this._pending.some((mutation) => mutation.key === key
            && (!currentEpochOnly
                || mutation.coalescingEpoch === this._coalescingEpoch));
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
            const ownerToken = {};
            this._activeOwnerToken = ownerToken;
            try {
                const result = await mutation.run(
                    mutation.intent, mutation.diagnostics, ownerToken);
                for (const waiter of mutation.waiters) {
                    waiter.resolve(result);
                }
            } catch (error) {
                for (const waiter of mutation.waiters) {
                    waiter.reject(error);
                }
            } finally {
                if (this._activeOwnerToken === ownerToken) {
                    this._activeOwnerToken = null;
                }
                this._active = null;
            }
        }
        this._flushDrainWaiters();
    }

    _flushDrainWaiters() {
        if (this.isBusy() || this._drainFlushScheduled) return;
        this._drainFlushScheduled = true;
        // Mutation waiter continuations may enqueue dependent writes. Give
        // those same-turn continuations one microtask to declare their work
        // before drain waiters observe a stable idle boundary.
        queueMicrotask(() => {
            this._drainFlushScheduled = false;
            if (this.isBusy()) return;
            const waiters = this._drainWaiters.splice(0);
            for (const resolve of waiters) {
                resolve();
            }
            this._onIdle?.();
        });
    }
}
