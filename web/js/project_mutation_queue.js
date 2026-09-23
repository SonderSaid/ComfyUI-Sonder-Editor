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
        if (this._runsInline(ownerToken)) {
            if (diagnostics && typeof diagnostics === "object") {
                diagnostics.coalescedCount = 1;
            }
            return Promise.resolve().then(() => run(intent, diagnostics, ownerToken));
        }

        // Under tail-only merging the epoch is a second guard, not the only one:
        // every sealing write today (Undo, Redo, a lane move) has a key of its
        // own and becomes the tail itself, so nothing could merge across it
        // anyway. Kept so a sealing write that shares a key still seals. Retire
        // the epoch once sealing is required to carry a unique key.
        if (sealCoalescing) this._coalescingEpoch += 1;
        const coalescingEpoch = this._coalescingEpoch;

        return new Promise((resolve, reject) => {
            const waiter = { resolve, reject };
            const existing = coalesce !== false
                ? this._tailSlot(key, coalescingEpoch)
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

    /**
     * The pending slot an enqueue with these options would merge into, or null
     * when it would take a slot of its own or run inline. The same decision
     * `enqueue` makes, asked beforehand, so a caller that keeps per-slot state
     * (the widget's undo entry) cannot disagree with the queue about it. The
     * slot is the queue's; read it, never write it.
     */
    coalesceTarget(key, { coalesce = true, ownerToken = null, sealCoalescing = false } = {}) {
        if (!key || coalesce === false || sealCoalescing || this._runsInline(ownerToken)) {
            return null;
        }
        return this._tailSlot(key, this._coalescingEpoch);
    }

    /**
     * Only the LAST pending slot is ever merged into. Merging into an earlier
     * one would send a gesture ahead of writes authored before it: a different
     * key enqueued between two members of a burst would land physically after
     * the merged write, so its Undo would revert the later member's change and
     * the Undo after that would be refused. Physical write order therefore
     * equals authoring order. A burst of one key still costs one write per
     * in-flight window, because nothing else enqueues while it is typed.
     */
    _tailSlot(key, coalescingEpoch) {
        const tail = this._pending[this._pending.length - 1];
        return tail && tail.key === key && tail.coalescingEpoch === coalescingEpoch
            ? tail : null;
    }

    _runsInline(ownerToken) {
        return !!ownerToken && !!this._active && ownerToken === this._activeOwnerToken;
    }

    hasPending() {
        return this._pending.length > 0;
    }

    // Any pending slot, not only the tail, so it no longer predicts whether an
    // enqueue coalesces -- `coalesceTarget` does. Only the queue's own tests
    // call it; remove it when they assert through `coalesceTarget` instead.
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
