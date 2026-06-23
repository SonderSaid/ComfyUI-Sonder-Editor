// Prompt Management panel — centered overlay for editing the scene-global
// prompt, the segment lane's sections (ranges + channels), the project-durable
// channel-labels toggle, and a PromptRelay payload preview.
//
// Module-host contract (fullscreen seam pattern): the host owns state,
// networking, and durable writes; this module owns its DOM/listeners and
// returns a cleanup handle. Host surface used:
//   activeScene, totalFrames, _settings, _updateSettings,
//   _promptChannelLabels, _projectDirName(), _timecodeMode/_frameToTimecode,
//   _resolveFrameConstraintForTemplate(_templateId),
//   _updateScenePrompt(text), _updatePromptSection(idx, fields),
//   _deletePromptSection(idx), _setSelectionToFrameRange(start, end),
//   _queuePromptSection(section), _addPromptSectionAfter(idx),
//   _addPromptSectionInFirstGap(), _applyPromptSetup(entry),
//   _fetchPromptPayload()/_fetchPromptHistory() -> Promise,
//   _getPromptTemplates()/_savePromptTemplate()/_deletePromptTemplate(),
//   _isPromptTrackLocked(), _isGlobalPromptTrackLocked()
// (The channel-labels toggle lives in Settings > Prompts via the settings
// host adapter, not here.)
//
// Improvements over the guide-management template: focus-aware Escape via
// keyboard_ownership at PRIORITY.OVERLAY (first Esc reverts the focused box
// without committing — closing paths suppress blur-commit), and the panel
// stays open after commits (rows refresh in place).

import {
    EDITOR_COLORS as COLORS,
    FONT,
    chromeButtonCss,
    chromeInputCss,
    chromeOverlayPanelCss,
    setButtonVariant,
} from "./editor_theme.js";
import {
    register as registerKeyboardConsumer,
    PRIORITY as KEY_PRIORITY,
} from "./keyboard_ownership.js";
import { composeSectionText, normalizeChannels } from "./prompt_composition.js";
import { notifySuccess, notifyWarning } from "./editor_notifications.js";

const WRITING_BREAK = "---";
const WRITING_DRAFT_TEXT_CAP = 20000;

/** Split a writing-mode draft into blocks on `---` marker lines. */
export function splitWritingDraft(draft) {
    const blocks = [];
    let current = [];
    for (const line of String(draft ?? "").split("\n")) {
        if (line.trim() === WRITING_BREAK) {
            blocks.push(current.join("\n").trim());
            current = [];
        } else {
            current.push(line);
        }
    }
    blocks.push(current.join("\n").trim());
    return blocks.filter(Boolean);
}

/** Min-first proportional allocator (audit F2 — min to every block FIRST so
 *  clamping can never overflow the budget): non-dirty chips share
 *  `total − Σdirty` proportionally by text length with largest-remainder. */
export function allocateWritingBlocks(blocks, total, minLen, existing = []) {
    const count = blocks.length;
    if (!count) return [];
    const result = blocks.map((_, i) => (existing[i]?.dirty
        ? { length: Math.max(minLen, existing[i].length | 0), dirty: true }
        : null));
    const dirtySum = result.reduce((sum, entry) => sum + (entry ? entry.length : 0), 0);
    const freeIdx = result.map((entry, i) => (entry === null ? i : -1)).filter((i) => i >= 0);
    if (!freeIdx.length) return result;
    const budget = Math.max(freeIdx.length * minLen, total - dirtySum);
    const extra = budget - freeIdx.length * minLen;
    const weights = freeIdx.map((i) => Math.max(1, blocks[i].length));
    const weightSum = weights.reduce((a, b) => a + b, 0);
    const raw = weights.map((w) => (extra * w) / weightSum);
    const floors = raw.map(Math.floor);
    let leftover = extra - floors.reduce((a, b) => a + b, 0);
    const byFraction = raw
        .map((value, k) => ({ k, frac: value - floors[k] }))
        .sort((a, b) => b.frac - a.frac);
    for (let n = 0; n < leftover; n += 1) floors[byFraction[n % byFraction.length].k] += 1;
    freeIdx.forEach((blockIdx, k) => {
        result[blockIdx] = { length: minLen + floors[k], dirty: false };
    });
    return result;
}

function makeBtn(label, title, variant = "secondary") {
    const btn = document.createElement("button");
    btn.textContent = label;
    btn.title = title || label;
    btn.style.cssText = chromeButtonCss();
    setButtonVariant(btn, variant);
    return btn;
}

function smallInput({ value = "", placeholder = "", width = "", numeric = false } = {}) {
    const input = document.createElement("input");
    input.type = numeric ? "number" : "text";
    input.value = value;
    input.placeholder = placeholder;
    input.style.cssText = `${chromeInputCss({ padding: "4px 6px" })}; ${width ? `width:${width};` : "flex:1; min-width:40px;"}`;
    return input;
}

// Paragraph-size prompt textarea with a browser-local persisted height
// (one shared height per kind via prompts.panel*BoxHeight). Enter commits,
// Shift+Enter inserts a newline — Esc is handled by the panel's focus-aware
// OVERLAY consumer, not here. Native `resize` is OFF — the corner grip
// disappears under the scrollbar once content overflows, so each box kind
// gets an explicit drag grip (makeHeightGrip) below it instead.
function promptBox(host, { value = "", placeholder = "", heightKey = "", title = "", defaultHeight = 56 } = {}) {
    const area = document.createElement("textarea");
    area.value = value;
    area.placeholder = placeholder;
    area.title = title ? `${title} — Enter commits, Shift+Enter inserts a newline` : "Enter commits, Shift+Enter inserts a newline";
    const persisted = heightKey ? (host._settings?.prompts?.[heightKey] || 0) : 0;
    area.style.cssText = `${chromeInputCss({ padding: "4px 6px" })}; flex:1; min-width:40px; resize: none; line-height: 1.4; min-height: 28px; height: ${persisted > 0 ? `${persisted}px` : `${defaultHeight}px`}; overflow-y: auto;`;
    area.dataset.sonderPromptBox = "1";
    if (heightKey) area.dataset.sonderBoxKind = heightKey;
    return area;
}

// Slim drag bar that resizes EVERY textarea of one height kind live and
// persists the shared height on release (prompts.panel*BoxHeight).
function makeHeightGrip(host, scopeEl, heightKey, { min = 28, max = 800, label = "Drag to resize" } = {}) {
    const grip = document.createElement("div");
    grip.title = label;
    grip.style.cssText = `
        height: 7px; margin: -2px 0 0; border-radius: 3px; cursor: ns-resize;
        background: ${COLORS.promptBorder}; opacity: 0.35; flex: 0 0 auto;
        transition: opacity 0.12s;
    `;
    grip.addEventListener("mouseenter", () => { grip.style.opacity = "0.8"; });
    grip.addEventListener("mouseleave", () => { grip.style.opacity = "0.35"; });
    grip.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        const boxes = [...scopeEl.querySelectorAll(`[data-sonder-box-kind="${heightKey}"]`)];
        if (!boxes.length) return;
        const startY = e.clientY;
        const startH = boxes[0].offsetHeight || min;
        let nextH = startH;
        grip.setPointerCapture(e.pointerId);
        const onMove = (ev) => {
            nextH = Math.max(min, Math.min(max, Math.round(startH + (ev.clientY - startY))));
            for (const box of boxes) box.style.height = `${nextH}px`;
        };
        const onUp = () => {
            grip.removeEventListener("pointermove", onMove);
            grip.removeEventListener("pointerup", onUp);
            grip.removeEventListener("pointercancel", onUp);
            if (nextH !== (host._settings?.prompts?.[heightKey] || 0)) {
                host._updateSettings({ prompts: { [heightKey]: nextH } });
            }
        };
        grip.addEventListener("pointermove", onMove);
        grip.addEventListener("pointerup", onUp);
        grip.addEventListener("pointercancel", onUp);
    });
    return grip;
}

export function mountPromptManagementPanel(host) {
    const backdrop = document.createElement("div");
    backdrop.style.cssText = `
        position: fixed; inset: 0; z-index: 10000;
        background: rgba(7,10,14,0.70);
        display: flex; align-items: center; justify-content: center;
    `;
    const panel = document.createElement("div");
    panel.style.cssText = chromeOverlayPanelCss({
        width: "min(900px, 92vw)", maxWidth: "900px", maxHeight: "82vh", padding: "0",
    }) + "display:flex; flex-direction:column; overflow:hidden;";
    backdrop.appendChild(panel);

    let mounted = true;
    // Esc/blur-commit guard (audit F1): the OVERLAY consumer fires on the
    // window-CAPTURE root BEFORE textarea handlers, and closing removes
    // focused boxes whose blur would otherwise COMMIT a cancelled edit.
    const guard = { suppressBlurCommit: false, focusedBox: null };
    const registerPromptBoxGuard = (area, revertValue) => {
        area.addEventListener("focus", () => {
            guard.focusedBox = { el: area, revert: revertValue() };
        });
        area.addEventListener("blur", () => {
            if (guard.focusedBox?.el === area) guard.focusedBox = null;
        });
    };

    const close = () => {
        if (!mounted) return;
        mounted = false;
        // Closing mid-edit must never commit via the removal-triggered blur
        guard.suppressBlurCommit = true;
        unregisterKeyboard();
        backdrop.remove();
        guard.suppressBlurCommit = false;
        if (host._promptPanelHandle === handle) host._promptPanelHandle = null;
    };

    const unregisterKeyboard = registerKeyboardConsumer({
        id: `sonder-prompt-panel-${Date.now().toString(36)}`,
        priority: KEY_PRIORITY.OVERLAY,
        keydown: (e) => {
            if (e.key !== "Escape") return false;
            const focused = guard.focusedBox;
            if (focused && document.activeElement === focused.el) {
                // First Esc: revert the box and drop focus WITHOUT committing;
                // panel stays open. A second Esc (no focused box) closes.
                focused.el.value = focused.revert;
                guard.suppressBlurCommit = true;
                focused.el.blur();
                guard.suppressBlurCommit = false;
                guard.focusedBox = null;
                return true;
            }
            close();
            return true;
        },
    });

    backdrop.addEventListener("mousedown", (e) => {
        if (e.target === backdrop) close();
    });

    // ── Writing-mode state (per project+scene; persisted browser-local) ──
    // An EMPTY persisted draft is treated as "no draft" (deep-merge settings
    // updates cannot delete map keys, so clearing stores an empty entry).
    const writingState = { key: "", draft: "", allocations: [] };
    const writingDraftKey = () => `${host._projectDirName?.() || ""}::${host.activeSceneId || ""}`;
    const reconstructDraftFromSections = () => {
        const sections = host.activeScene?.prompt_sections || [];
        writingState.draft = sections
            .map((s) => composeSectionText(normalizeChannels(s.channels, s.prompt), false))
            .join(`\n${WRITING_BREAK}\n`);
        writingState.allocations = sections.map((s) => ({
            length: Math.max(1, (s.end_frame || 0) - (s.start_frame || 0)),
            dirty: false,
        }));
    };
    const loadWritingState = () => {
        const key = writingDraftKey();
        if (writingState.key === key) return;
        writingState.key = key;
        const saved = host._settings?.prompts?.writingDraftByProjectScene?.[key];
        if (saved && String(saved.draft || "").trim()) {
            writingState.draft = String(saved.draft || "");
            writingState.allocations = (saved.allocations || []).map((a) => ({
                length: Math.max(0, parseInt(a?.length, 10) || 0),
                dirty: !!a?.dirty,
            }));
        } else {
            reconstructDraftFromSections();
        }
    };
    const saveWritingState = () => {
        if (!writingState.key) return;
        if (writingState.draft.length > WRITING_DRAFT_TEXT_CAP) {
            writingState.draft = writingState.draft.slice(0, WRITING_DRAFT_TEXT_CAP);
            notifyWarning("Writing draft truncated to 20k characters.", { source: "prompt-writing-draft-cap" });
        }
        const map = { ...(host._settings?.prompts?.writingDraftByProjectScene || {}) };
        map[writingState.key] = {
            ts: Date.now(),
            draft: writingState.draft,
            allocations: writingState.allocations.map((a) => ({ length: a.length, dirty: !!a.dirty })),
        };
        host._updateSettings({ prompts: { writingDraftByProjectScene: map } });
    };
    const clearWritingState = () => {
        const map = { ...(host._settings?.prompts?.writingDraftByProjectScene || {}) };
        map[writingState.key] = { ts: Date.now(), draft: "", allocations: [] };
        host._updateSettings({ prompts: { writingDraftByProjectScene: map } });
        writingState.key = ""; // force reload (reconstruct) on next render
    };

    // ── Header ───────────────────────────────────────────────────────
    const header = document.createElement("div");
    header.style.cssText = `
        display:flex; align-items:center; justify-content:space-between;
        padding: 12px 18px; border-bottom: 1px solid ${COLORS.promptBorder};
        font-family:${FONT.sans};
    `;
    const title = document.createElement("div");
    title.style.cssText = `font-size:13px; font-weight:600; color:${COLORS.text};`;
    title.textContent = "Prompt Management";
    const closeBtn = makeBtn("Close", "Close (Esc)", "subtle");
    closeBtn.addEventListener("click", close);
    header.append(title, closeBtn);
    panel.appendChild(header);

    const body = document.createElement("div");
    body.style.cssText = `
        flex:1; overflow-y:auto; padding: 12px 18px; display:flex;
        flex-direction:column; gap: 14px; font-family:${FONT.sans};
    `;
    panel.appendChild(body);

    const sectionTitle = (text) => {
        const el = document.createElement("div");
        el.style.cssText = `font-size:11px; font-weight:600; color:${COLORS.textMuted}; text-transform:uppercase; letter-spacing:0.4px;`;
        el.textContent = text;
        return el;
    };

    // ── Writing mode view: narrative draft + --- splits + allocation strip ──
    const renderWritingView = (bodyEl, sectionsLocked) => {
        loadWritingState();
        const scene = host.activeScene;
        const totalFrames = scene?.duration_frames || host.totalFrames || 0;
        const step = host._resolveFrameConstraintForTemplate?.(host._templateId)?.step;
        const minLen = Math.max(1, parseInt(step, 10) || 1);
        const globalLocked = host._isGlobalPromptTrackLocked();
        const applyBlocked = sectionsLocked || globalLocked;

        bodyEl.appendChild(sectionTitle("Writing Mode — narrative draft"));
        const hint = document.createElement("div");
        hint.style.cssText = `font-size:10px; color:${COLORS.textDim};`;
        hint.textContent = `Write freely; a line containing only ${WRITING_BREAK} splits sections. Apply replaces the lane's sections (undoable; all text lands in the Visual channel).`;
        bodyEl.appendChild(hint);

        const draftArea = promptBox(host, {
            value: writingState.draft,
            placeholder: "Write your narrative here…\n---\n…and split it into sections with --- lines.",
            heightKey: "panelDraftBoxHeight",
            title: "Narrative draft (auto-saved per scene)",
            defaultHeight: 160,
        });
        draftArea.style.minHeight = "120px";
        // In the column-flex body, flex:1 would fight manual height — the
        // draft box must own its height for the grip to work
        draftArea.style.flex = "0 0 auto";
        registerPromptBoxGuard(draftArea, () => writingState.draft);
        let draftTimer = null;
        draftArea.addEventListener("input", () => {
            writingState.draft = draftArea.value;
            if (draftTimer) clearTimeout(draftTimer);
            draftTimer = setTimeout(() => updateStrip(), 300);
        });
        draftArea.addEventListener("blur", () => {
            if (guard.suppressBlurCommit) return;
            writingState.draft = draftArea.value;
            saveWritingState();
        });
        draftArea.addEventListener("keydown", (e) => e.stopPropagation());
        bodyEl.appendChild(draftArea);
        bodyEl.appendChild(makeHeightGrip(host, bodyEl, "panelDraftBoxHeight", { min: 120, label: "Drag to resize the draft box (persists)" }));

        const toolRow = document.createElement("div");
        toolRow.style.cssText = "display:flex; gap:6px; align-items:center; flex-wrap:wrap;";
        const splitBtn = makeBtn("Split here", "Insert a --- section break at the cursor");
        splitBtn.addEventListener("click", () => {
            const pos = draftArea.selectionStart ?? draftArea.value.length;
            draftArea.value = `${draftArea.value.slice(0, pos)}\n${WRITING_BREAK}\n${draftArea.value.slice(pos)}`;
            writingState.draft = draftArea.value;
            draftArea.focus();
            draftArea.selectionStart = draftArea.selectionEnd = pos + WRITING_BREAK.length + 2;
            saveWritingState();
            updateStrip();
        });
        const equalizeBtn = makeBtn("Equalize", "Reset all lengths to an equal split of the scene");
        const resetBtn = makeBtn("Reset from sections", "Rebuild the draft + lengths from the lane's current sections");
        resetBtn.addEventListener("click", () => {
            reconstructDraftFromSections();
            saveWritingState();
            render();
        });
        const applyBtn = makeBtn(applyBlocked ? "Apply (locked)" : "Apply", "Replace the lane's sections with the draft blocks (undoable)", "primary");
        toolRow.append(splitBtn, equalizeBtn, resetBtn, applyBtn);
        bodyEl.appendChild(toolRow);

        const readout = document.createElement("div");
        readout.style.cssText = `font-size:10px; color:${COLORS.textDim};`;
        bodyEl.appendChild(readout);
        const strip = document.createElement("div");
        strip.style.cssText = "display:flex; flex-direction:column; gap:4px;";
        bodyEl.appendChild(strip);

        const chipLengths = () => writingState.allocations.reduce((sum, a) => sum + (a?.length || 0), 0);

        // The actual laid-out duration uses max(minLen, allocation) per block,
        // so it can exceed the raw chip sum when an allocation is below the
        // template minimum — this is the value the scene must fit/grow to.
        const laidOutTotal = (blocks) =>
            blocks.reduce((sum, _t, i) =>
                sum + Math.max(minLen, writingState.allocations[i]?.length ?? minLen), 0);

        const updateReadout = (blocks) => {
            const total = chipLengths();
            const requiredTotal = laidOutTotal(blocks);
            const willExtend = blocks.length > 0 && requiredTotal > totalFrames;
            readout.textContent = blocks.length
                ? `Sections: ${blocks.length} — total ${total}f of ${totalFrames}f (${Math.max(0, totalFrames - total)}f remaining; min ${minLen}f per section)`
                : "No sections yet — the whole draft is one block until you add --- lines.";
            if (willExtend) readout.textContent += ` — Apply will extend the scene to ${requiredTotal}f.`;
            readout.style.color = COLORS.textDim;
            // Over-budget no longer blocks — Apply extends the scene instead
            // (which also satisfies the per-section minimum). Only a locked
            // lane or an empty draft can block.
            if (applyBlocked) applyBtn.textContent = "Apply (locked)";
            else applyBtn.textContent = willExtend ? `Apply & Extend Scene to ${requiredTotal}f` : "Apply";
            applyBtn.disabled = applyBlocked || !blocks.length;
        };

        const updateStrip = () => {
            const blocks = splitWritingDraft(writingState.draft);
            if (blocks.length !== writingState.allocations.length) {
                writingState.allocations = allocateWritingBlocks(
                    blocks, totalFrames, minLen, writingState.allocations);
            }
            strip.textContent = "";
            blocks.forEach((text, i) => {
                const chip = document.createElement("div");
                chip.style.cssText = `display:flex; gap:8px; align-items:center; font-size:10px; color:${COLORS.text};`;
                const preview = document.createElement("span");
                preview.style.cssText = "flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;";
                preview.textContent = `${i + 1}. ${text}`;
                preview.title = text;
                const lengthInput = smallInput({ value: String(writingState.allocations[i]?.length ?? minLen), numeric: true, width: "64px" });
                lengthInput.min = String(minLen);
                lengthInput.title = `Length in frames (min ${minLen})`;
                const timecodeHint = document.createElement("span");
                timecodeHint.style.cssText = `flex-shrink:0; font-family:${FONT.mono}; color:${COLORS.textDim}; min-width:60px;`;
                const syncHint = () => {
                    const frames = writingState.allocations[i]?.length ?? 0;
                    timecodeHint.textContent = host._timecodeMode === "timecode"
                        ? host._frameToTimecode(frames)
                        : `${frames}f`;
                };
                lengthInput.addEventListener("change", () => {
                    const value = Math.max(minLen, parseInt(lengthInput.value, 10) || minLen);
                    lengthInput.value = String(value);
                    writingState.allocations[i] = { length: value, dirty: true };
                    syncHint();
                    updateReadout(blocks);
                    saveWritingState();
                });
                lengthInput.addEventListener("keydown", (e) => e.stopPropagation());
                syncHint();
                chip.append(preview, lengthInput, timecodeHint);
                strip.appendChild(chip);
            });
            updateReadout(blocks);
        };

        equalizeBtn.addEventListener("click", () => {
            const blocks = splitWritingDraft(writingState.draft);
            writingState.allocations = allocateWritingBlocks(
                blocks.map(() => " "), totalFrames, minLen, []); // equal weights
            saveWritingState();
            updateStrip();
        });

        applyBtn.addEventListener("click", async () => {
            if (applyBtn.disabled) return;
            const blocks = splitWritingDraft(writingState.draft);
            let cursor = 0;
            const sections = blocks.map((text, i) => {
                const length = Math.max(minLen, writingState.allocations[i]?.length ?? minLen);
                const section = {
                    start_frame: cursor,
                    end_frame: cursor + length,
                    channels: { visual: text, speech: "", sounds: "" },
                };
                cursor += length;
                return section;
            });
            const sceneDur = host.activeScene?.duration_frames || host.totalFrames || 0;
            const extendDurationTo = cursor > sceneDur ? cursor : 0;
            await host._applyPromptSetup({
                global: host.activeScene?.prompt || "",
                sections,
                extendDurationTo,
            });
            clearWritingState();
            notifySuccess(
                extendDurationTo
                    ? `Applied ${sections.length} section(s) and extended the scene to ${extendDurationTo}f.`
                    : `Applied ${sections.length} section(s) from the draft.`,
                { source: "prompt-writing-apply" });
            render();
        });

        updateStrip();
    };

    const render = () => {
        if (!mounted) return;
        body.textContent = "";
        const scene = host.activeScene;
        if (!scene) {
            body.appendChild(sectionTitle("No active scene"));
            return;
        }
        const sectionsLocked = host._isPromptTrackLocked();
        const globalLocked = host._isGlobalPromptTrackLocked();

        // ── Global prompt (auto-commits on Enter/blur; no Save button) ─
        body.appendChild(sectionTitle(`Global Prompt (always-on)${globalLocked ? " — locked" : ""}`));
        const globalRow = document.createElement("div");
        globalRow.style.cssText = "display:flex; gap:6px; align-items:flex-start;";
        const globalInput = promptBox(host, {
            value: scene.prompt || "",
            placeholder: "Scene-global prompt (style, identity, location)…",
            heightKey: "panelGlobalBoxHeight",
            title: "Global prompt — auto-commits on Enter or focus loss",
        });
        globalInput.disabled = globalLocked;
        registerPromptBoxGuard(globalInput, () => host.activeScene?.prompt || "");
        const commitGlobal = async () => {
            if (globalLocked || guard.suppressBlurCommit) return;
            if (globalInput.value === (host.activeScene?.prompt || "")) return;
            await host._updateScenePrompt(globalInput.value);
        };
        globalInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                commitGlobal().catch(() => {});
            }
            e.stopPropagation();
        });
        globalInput.addEventListener("blur", () => { commitGlobal().catch(() => {}); });
        globalRow.append(globalInput);
        body.appendChild(globalRow);
        body.appendChild(makeHeightGrip(host, body, "panelGlobalBoxHeight", { label: "Drag to resize the global prompt box (persists)" }));

        // ── Mode toggle: Structured | Writing (sticky browser-local) ──
        const mode = host._settings?.prompts?.panelMode === "writing" ? "writing" : "structured";
        const modeRow = document.createElement("div");
        modeRow.style.cssText = "display:flex; gap:4px; align-items:center;";
        for (const [value, label] of [["structured", "Structured"], ["writing", "Writing"]]) {
            const btn = makeBtn(label, value === "writing"
                ? "Narrative drafting: write freely, split with --- markers, allocate lengths, Apply"
                : "Per-section editing: ranges, channels, queueing", mode === value ? "primary" : "subtle");
            btn.addEventListener("click", () => {
                if (mode === value) return;
                if (mode === "writing") saveWritingState();
                host._updateSettings({ prompts: { panelMode: value } });
                render();
            });
            modeRow.appendChild(btn);
        }
        body.appendChild(modeRow);

        if (mode === "writing") {
            renderWritingView(body, sectionsLocked);
            return;
        }

        // ── Sections ─────────────────────────────────────────────────
        const sectionsHeader = document.createElement("div");
        sectionsHeader.style.cssText = "display:flex; align-items:center; justify-content:space-between; gap:12px;";
        sectionsHeader.appendChild(sectionTitle(`Prompt Sections (${(scene.prompt_sections || []).length})${sectionsLocked ? " — track locked" : ""}`));
        // Sticky batch toggle for the per-row Queue buttons (browser-local;
        // Settings > Prompts holds the same key as the default)
        const batchToggle = document.createElement("label");
        batchToggle.style.cssText = `display:flex; gap:6px; align-items:center; font-size:10px; color:${COLORS.textMuted}; cursor:pointer;`;
        batchToggle.title = "On: queued sections auto-chunk through the batch path past the chunk budget. Off: one job for the whole range.";
        const batchCheck = document.createElement("input");
        batchCheck.type = "checkbox";
        batchCheck.checked = host._settings?.prompts?.queueSectionBatch !== false;
        batchCheck.addEventListener("change", () => {
            host._updateSettings({ prompts: { queueSectionBatch: batchCheck.checked } });
        });
        const batchText = document.createElement("span");
        batchText.textContent = "Queue as batch";
        batchToggle.append(batchCheck, batchText);
        sectionsHeader.appendChild(batchToggle);
        body.appendChild(sectionsHeader);
        const list = document.createElement("div");
        list.style.cssText = "display:flex; flex-direction:column; gap:6px;";
        const sections = scene.prompt_sections || [];
        if (!sections.length) {
            const empty = document.createElement("div");
            empty.style.cssText = `font-size:11px; color:${COLORS.textDim};`;
            empty.textContent = "No sections. Double-click the Prompt lane to create one.";
            list.appendChild(empty);
        }
        sections.forEach((section, idx) => {
            const row = document.createElement("div");
            row.style.cssText = `
                display:grid; grid-template-columns: 58px 58px 2fr 1.2fr 1.2fr auto auto auto auto;
                gap:6px; align-items:start; padding:6px 8px;
                background:${COLORS.panel}; border:1px solid ${COLORS.promptBorder}; border-radius:4px;
            `;
            const startInput = smallInput({ value: String(section.start_frame ?? 0), numeric: true, width: "100%" });
            const endInput = smallInput({ value: String(section.end_frame ?? 0), numeric: true, width: "100%" });
            startInput.title = "Start frame";
            endInput.title = "End frame (exclusive)";
            const channels = normalizeChannels(section.channels, section.prompt);
            const visualInput = promptBox(host, { value: channels.visual, placeholder: "Visual…", heightKey: "panelChannelBoxHeight", title: "Visual channel" });
            const speechInput = promptBox(host, { value: channels.speech, placeholder: "Speech…", heightKey: "panelChannelBoxHeight", title: "Speech channel" });
            const soundsInput = promptBox(host, { value: channels.sounds, placeholder: "Sounds…", heightKey: "panelChannelBoxHeight", title: "Sounds channel" });
            registerPromptBoxGuard(visualInput, () => channels.visual);
            registerPromptBoxGuard(speechInput, () => channels.speech);
            registerPromptBoxGuard(soundsInput, () => channels.sounds);

            const commitRange = async () => {
                if (guard.suppressBlurCommit) return;
                const start = parseInt(startInput.value, 10);
                const end = parseInt(endInput.value, 10);
                if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
                    render();
                    return;
                }
                if (start === section.start_frame && end === section.end_frame) return;
                await host._updatePromptSection(idx, { start_frame: start, end_frame: end });
                render(); // indices may have re-sorted — rebuild rows
            };
            const commitChannels = async () => {
                if (guard.suppressBlurCommit) return;
                const next = {
                    visual: visualInput.value.trim(),
                    speech: speechInput.value.trim(),
                    sounds: soundsInput.value.trim(),
                };
                if (next.visual === channels.visual && next.speech === channels.speech && next.sounds === channels.sounds) return;
                await host._updatePromptSection(idx, { channels: next });
                render();
            };
            const onEnterBlur = (input, commit) => {
                input.addEventListener("keydown", (e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        commit().catch(() => render());
                    }
                    e.stopPropagation();
                });
                input.addEventListener("blur", () => commit().catch(() => render()));
            };
            for (const el of [startInput, endInput]) { el.disabled = sectionsLocked; onEnterBlur(el, commitRange); }
            for (const el of [visualInput, speechInput, soundsInput]) { el.disabled = sectionsLocked; onEnterBlur(el, commitChannels); }

            const selectBtn = makeBtn("Select", "Set selection to this section");
            selectBtn.addEventListener("click", () => {
                host._setSelectionToFrameRange(section.start_frame || 0, section.end_frame || 0);
            });
            const queueBtn = makeBtn("Queue", "Queue this prompt section (Batch toggle picks chunked vs single job)");
            queueBtn.addEventListener("click", () => { host._queuePromptSection(section).catch(() => {}); });
            const addAfterBtn = makeBtn("+", "Insert a new section after this one (fills the gap to the next section)");
            addAfterBtn.disabled = sectionsLocked;
            addAfterBtn.addEventListener("click", async () => {
                if (sectionsLocked) return;
                const created = await host._addPromptSectionAfter(idx).catch(() => false);
                if (created) render();
            });
            const deleteBtn = makeBtn("✕", "Delete this section", "danger");
            deleteBtn.disabled = sectionsLocked;
            deleteBtn.addEventListener("click", async () => {
                if (sectionsLocked) return;
                if (!confirm("Delete this prompt section?")) return;
                await host._deletePromptSection(idx);
                render();
            });

            row.append(startInput, endInput, visualInput, speechInput, soundsInput, selectBtn, queueBtn, addAfterBtn, deleteBtn);
            list.appendChild(row);
        });
        body.appendChild(list);
        if (sections.length) {
            body.appendChild(makeHeightGrip(host, body, "panelChannelBoxHeight", { label: "Drag to resize all channel boxes (persists)" }));
        }

        const addSectionBtn = makeBtn("Add Section", "Create a section in the first free gap on the lane");
        addSectionBtn.disabled = sectionsLocked;
        addSectionBtn.addEventListener("click", async () => {
            if (sectionsLocked) return;
            const created = await host._addPromptSectionInFirstGap().catch(() => false);
            if (created) render();
        });
        body.appendChild(addSectionBtn);

        // ── Templates (browser-local library) ────────────────────────
        body.appendChild(sectionTitle("Prompt Templates (browser-local)"));
        const templateSaveRow = document.createElement("div");
        templateSaveRow.style.cssText = "display:flex; gap:6px; align-items:center;";
        const templateNameInput = smallInput({ placeholder: "Template name…" });
        const templateSaveBtn = makeBtn("Save Current as Template", "Snapshot the global prompt + sections into the browser-local library");
        templateSaveBtn.addEventListener("click", () => {
            if (!templateNameInput.value.trim()) return;
            host._savePromptTemplate(templateNameInput.value);
            templateNameInput.value = "";
            render();
        });
        templateSaveRow.append(templateNameInput, templateSaveBtn);
        body.appendChild(templateSaveRow);
        const templates = host._getPromptTemplates();
        if (templates.length) {
            const templateList = document.createElement("div");
            templateList.style.cssText = "display:flex; flex-direction:column; gap:4px;";
            for (const template of templates) {
                const row = document.createElement("div");
                row.style.cssText = `display:flex; gap:6px; align-items:center; font-size:11px; color:${COLORS.text};`;
                const label = document.createElement("span");
                label.style.cssText = "flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;";
                label.textContent = `${template.name} — ${template.sections.length} section(s)${template.global ? ", global" : ""}`;
                label.title = template.global || "";
                const applyBtn = makeBtn("Apply", "Replace the scene's prompt state with this template (undoable)");
                applyBtn.addEventListener("click", async () => {
                    await host._applyPromptSetup(template);
                    render();
                });
                const removeBtn = makeBtn("✕", "Delete this template", "danger");
                removeBtn.addEventListener("click", () => {
                    host._deletePromptTemplate(template.id);
                    render();
                });
                row.append(label, applyBtn, removeBtn);
                templateList.appendChild(row);
            }
            body.appendChild(templateList);
        }

        // ── History (Prompt Saver — captured at enqueue) ─────────────
        body.appendChild(sectionTitle("Prompt History (captured at enqueue)"));
        const historyWrap = document.createElement("div");
        historyWrap.style.cssText = "display:flex; flex-direction:column; gap:4px;";
        const historyBtn = makeBtn("Load History", "Fetch the project's enqueue-time prompt history");
        historyBtn.addEventListener("click", async () => {
            historyBtn.disabled = true;
            try {
                const entries = await host._fetchPromptHistory();
                historyWrap.textContent = "";
                if (!entries.length) {
                    const empty = document.createElement("div");
                    empty.style.cssText = `font-size:11px; color:${COLORS.textDim};`;
                    empty.textContent = "No history yet — entries are captured when jobs are queued.";
                    historyWrap.appendChild(empty);
                    return;
                }
                for (const entry of entries) {
                    const item = document.createElement("div");
                    item.style.cssText = "display:flex; flex-direction:column; gap:2px;";
                    const row = document.createElement("div");
                    row.style.cssText = `display:flex; gap:6px; align-items:center; font-size:11px; color:${COLORS.text};`;
                    const when = String(entry.ts || "").replace("T", " ").slice(0, 19);
                    const label = document.createElement("span");
                    label.style.cssText = "flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; cursor:pointer;";
                    const preview = entry.global || entry.sections?.[0]?.channels?.visual || "";
                    label.textContent = `${when} — ${(entry.sections || []).length} section(s) — ${preview}`;
                    label.title = "Click to expand the entry's global + per-section texts";
                    // Expandable detail: global + each section's composed text
                    const detail = document.createElement("div");
                    detail.style.cssText = `display:none; flex-direction:column; gap:2px; padding:4px 8px 6px 14px; border-left:2px solid ${COLORS.promptBorder};`;
                    const addDetailLine = (text) => {
                        const line = document.createElement("div");
                        line.textContent = text;
                        line.style.cssText = `font-size:10px; line-height:1.4; color:${COLORS.textDim}; word-break:break-word; white-space:pre-wrap;`;
                        detail.appendChild(line);
                    };
                    if (entry.global) addDetailLine(`Global: ${entry.global}`);
                    for (const s of entry.sections || []) {
                        const composed = composeSectionText(normalizeChannels(s.channels, s.prompt), false);
                        addDetailLine(`[${s.start_frame ?? 0}–${s.end_frame ?? 0}] ${composed || "(empty)"}`);
                    }
                    label.addEventListener("click", () => {
                        detail.style.display = detail.style.display === "none" ? "flex" : "none";
                    });
                    const applyBtn = makeBtn("Apply", "Replace the scene's prompt state with this entry (undoable)");
                    applyBtn.addEventListener("click", async () => {
                        await host._applyPromptSetup(entry);
                        render();
                    });
                    row.append(label, applyBtn);
                    item.append(row, detail);
                    historyWrap.appendChild(item);
                }
            } finally {
                historyBtn.disabled = false;
            }
        });
        body.appendChild(historyBtn);
        body.appendChild(historyWrap);

        // ── Relay payload preview ────────────────────────────────────
        body.appendChild(sectionTitle("PromptRelay Payload Preview"));
        const previewNote = document.createElement("div");
        previewNote.style.cssText = `font-size:10px; color:${COLORS.textDim};`;
        previewNote.textContent = "Full-scene window — execution payloads rebase tags/lengths to the render window, so this preview is structural, not literal.";
        body.appendChild(previewNote);
        const previewWrap = document.createElement("div");
        previewWrap.style.cssText = "display:flex; flex-direction:column; gap:4px;";
        const previewBtn = makeBtn("Refresh Preview", "Fetch the resolved payload from the backend");
        previewBtn.addEventListener("click", async () => {
            previewBtn.disabled = true;
            try {
                const payload = await host._fetchPromptPayload();
                previewWrap.textContent = "";
                if (!payload) {
                    const fail = document.createElement("div");
                    fail.style.cssText = `font-size:11px; color:${COLORS.dangerText};`;
                    fail.textContent = "Failed to fetch payload.";
                    previewWrap.appendChild(fail);
                    return;
                }
                const lines = [
                    ["source", payload.source],
                    ["global_prompt", payload.relay?.global_prompt ?? ""],
                    ["smart_prompt", payload.relay?.smart_prompt ?? ""],
                    ["local_prompts", payload.relay?.local_prompts ?? ""],
                    ["segment_lengths", payload.relay?.segment_lengths ?? ""],
                ];
                for (const [key, value] of lines) {
                    const line = document.createElement("div");
                    line.style.cssText = `font-size:10px; font-family:${FONT.mono || "monospace"}; color:${COLORS.text}; word-break:break-word; white-space:pre-wrap;`;
                    line.textContent = `${key}: ${value}`;
                    previewWrap.appendChild(line);
                }
            } finally {
                previewBtn.disabled = false;
            }
        });
        body.appendChild(previewBtn);
        body.appendChild(previewWrap);
    };

    render();
    document.body.appendChild(backdrop);

    const handle = {
        element: backdrop,
        refresh: render,
        cleanup: close,
        isMounted: () => mounted,
    };
    return handle;
}
