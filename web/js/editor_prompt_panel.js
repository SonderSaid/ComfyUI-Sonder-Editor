// Prompt Management panel — centered overlay for editing the scene-global
// prompt, the segment lane's sections (ranges + channels), the project-durable
// channel-template-aware prompt state and a PromptRelay payload preview.
//
// Module-host contract (fullscreen seam pattern): the host owns state,
// networking, and durable writes; this module owns its DOM/listeners and
// returns a cleanup handle. Host surface used:
//   activeScene, totalFrames, _settings, _updateSettings,
//   _projectDirName(), _timecodeMode/_frameToTimecode,
//   _resolveFrameConstraintForTemplate(_templateId),
//   _updateScenePrompt(text), _updatePromptSection(idx, fields),
//   _deletePromptSection(idx), _setSelectionToFrameRange(start, end),
//   _queuePromptSection(section), _addPromptSectionAfter(idx),
//   _addPromptSectionInFirstGap(), _applyPromptSetup(entry),
//   _fetchPromptPayload()/_fetchPromptHistory() -> Promise,
//   _getPromptTemplates()/_savePromptTemplate()/_deletePromptTemplate(),
//   _isPromptTrackLocked(), _isGlobalPromptTrackLocked()
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
import {
    composeSectionText,
    joinChannelHeaders,
    normalizeChannels,
    sectionInheritsGlobal,
    splitChannelHeaders,
} from "./prompt_composition.js";
import { globalChannelKeys, templateChannelKeys } from "./prompt_channel_templates.js";
import {
    configurePromptAttachment,
    createAttachmentChannelProjections,
    createPromptProjectionBox,
    createPromptDocumentEditor,
    createScopeChipRow,
    installPromptContextMenu,
    joinWritingSectionDocuments,
    normalizePromptAttachments,
    normalizePromptDocument,
    promptAttachmentAnchoredChannels,
    propagateLinkedPromptAttachment,
    promptDocumentText,
    resolveReferenceAttachmentIdentity,
    sceneWithDraftGlobal,
    sceneWithDraftSection,
    splitPromptDocumentChannels,
    splitWritingPromptDocument,
    setPromptAttachmentCapabilityEnabled,
    subjectSourceEligibility,
} from "./prompt_context_chips.js";
import { resolveReferenceVerdicts } from "./reference_resolution.js";
import { getProjectVersion } from "./api_client.js";
import { notifySuccess, notifyWarning } from "./editor_notifications.js";
import {
    buildPromptContextDiagnostics,
    promptContextDiagnosticTitle,
} from "./prompt_context_diagnostics.js";

const WRITING_BREAK = "---";
const WRITING_DRAFT_TEXT_CAP = 20000;

/** Find every persisted authoring seam that names a Reference-backed Subject. */
export function referenceBackedSubjectDependents(subjectId, scenes = []) {
    const counts = { reference_chips: 0, vocal_events: 0, audio_speaker_bindings: 0 };
    const visit = (value) => {
        if (Array.isArray(value)) {
            value.forEach(visit);
            return;
        }
        if (!value || typeof value !== "object") return;
        if (Array.isArray(value.semantic_unit_ids)
                && value.semantic_unit_ids.map(String).includes(String(subjectId))) {
            counts.reference_chips += 1;
        }
        if (Array.isArray(value.subject_ids)
                && value.subject_ids.map(String).includes(String(subjectId))) {
            counts.vocal_events += 1;
        }
        if (String(value.audio_speaker_subject_id || "") === String(subjectId)) {
            counts.audio_speaker_bindings += 1;
        }
        Object.values(value).forEach(visit);
    };
    visit(Array.isArray(scenes) ? scenes : []);
    return counts;
}

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
// (one shared height per kind via prompts.panel*BoxHeight). Enter and focus
// loss commit, Shift+Enter inserts a newline — Esc is handled by the panel's focus-aware
// OVERLAY consumer, not here. Native `resize` is OFF — the corner grip
// disappears under the scrollbar once content overflows, so each box kind
// gets an explicit drag grip (makeHeightGrip) below it instead.
//
// `flex: 0 0 auto` is load-bearing for the Writing draft: it is a direct child
// of the panel's column-flex body, where shrink pressure can silently override
// the height the grip writes. The grid-hosted channel boxes also use this
// helper so every persisted height is restored through one seam.
function applyBoxHeight(host, element, heightKey, fallback) {
    const persisted = Number(host._settings?.prompts?.[heightKey]);
    const height = Number.isFinite(persisted) && persisted > 0 ? persisted : fallback;
    element.style.height = `${height}px`;
    element.dataset.sonderBoxKind = heightKey;
    return element;
}

function appendPromptContextHint(host, parent) {
    if (host._settings?.prompts?.contextMenuHintDismissed) return;
    const hint = document.createElement("div");
    hint.style.cssText = `display:flex;gap:6px;align-items:center;font-size:9px;color:${COLORS.textDim};`;
    const text = document.createElement("span");
    text.textContent = "Right-click a prompt box, or press Shift+F10, to insert Context and Writing aids.";
    const dismiss = makeBtn("Got it", "Dismiss this prompt-authoring hint");
    dismiss.addEventListener("mousedown", (event) => event.preventDefault());
    dismiss.addEventListener("click", () => {
        host._updateSettings({ prompts: { contextMenuHintDismissed: true } });
        hint.remove();
    });
    hint.append(text, dismiss);
    parent.appendChild(hint);
}

// Slim drag bar that resizes EVERY textarea of one height kind live and
// persists the shared height on release (prompts.panel*BoxHeight).
function makeHeightGrip(host, scopeEl, heightKey, { min = 28, max = 800, label = "Drag to resize" } = {}) {
    const grip = document.createElement("div");
    grip.title = label;
    grip.textContent = "•••";
    grip.style.cssText = `
        height: 7px; margin: -2px 0 0; border-radius: 3px; cursor: ns-resize;
        background: ${COLORS.promptBorder}; opacity: 0.35; flex: 0 0 auto;
        transition: opacity 0.12s; display:flex; align-items:center;
        justify-content:center; color:${COLORS.text}; font:9px/7px ${FONT.sans};
        letter-spacing:2px; overflow:hidden;
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
        width: "min(1320px, 96vw)", maxWidth: "1320px", maxHeight: "86vh", padding: "0",
    }) + "display:flex; flex-direction:column; overflow:hidden;";
    backdrop.appendChild(panel);

    let mounted = true;
    // Esc/blur-commit guard (audit F1): the OVERLAY consumer fires on the
    // window-CAPTURE root BEFORE textarea handlers, and closing removes
    // focused boxes whose blur would otherwise COMMIT a cancelled edit.
    const guard = { suppressBlurCommit: false, focusedBox: null };
    const attachmentLabelFor = (attachment) => resolveReferenceAttachmentIdentity(attachment, {
        scene: host.activeScene,
        references: host._references || [],
        semanticUnits: host._promptSemanticUnits || [],
    });
    const registerPromptBoxGuard = (area, revertValue) => {
        area.addEventListener("focus", () => {
            const structuredState = area.promptState;
            guard.focusedBox = { el: area,
                revert: structuredState || revertValue(),
                structured: !!structuredState };
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
        for (const editor of backdrop.querySelectorAll("[data-sonder-prompt-box='1']")) {
            editor._sonderPromptContextMenuCleanup?.();
        }
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
                if (focused.structured) focused.el.promptState = focused.revert;
                else focused.el.value = focused.revert;
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
    const writingState = {
        key: "", draft: "", document: null, attachments: [],
        allocations: [], blockMeta: [], baseModifiedAt: "",
    };
    const documentNodeIds = (documentValue) => normalizePromptDocument(documentValue).nodes
        .map((node) => String(node.node_id || "")).filter(Boolean);
    const scopedAttachmentIdentity = (attachment) => {
        const semantic = structuredClone(attachment || {});
        // Split clones deliberately receive distinct authored object ids but share
        // an emission group. They coalesce only while every semantic field still
        // agrees; an edited clone remains present so compilation can surface the
        // conflicting emission instead of silently discarding either choice.
        delete semantic.attachment_id;
        return JSON.stringify(semantic);
    };
    const coalesceScopedAttachments = (values) => {
        const result = [];
        const seen = new Set();
        for (const attachment of normalizePromptAttachments(values)) {
            const identity = scopedAttachmentIdentity(attachment);
            if (seen.has(identity)) continue;
            seen.add(identity); result.push(attachment);
        }
        return result;
    };
    const cloneSplitScopedAttachments = (values) => normalizePromptAttachments(values)
        .filter((value) => !["shot", "timestamp"].includes(value.kind))
        .map((value) => ({ ...structuredClone(value),
            attachment_id: globalThis.crypto?.randomUUID?.().replaceAll("-", "")
                || `${Date.now()}${Math.random()}`.replaceAll(".", "") }));
    const writingDraftKey = () => `${host._projectDirName?.() || ""}::${host.activeSceneId || ""}`;
    // Writing mode is one flat text, and `key:` headers are what let it carry
    // every channel rather than just one. `---` still splits sections; headers
    // split channels within a section. A whole MiniMax-format model output
    // therefore pastes in and arranges itself.
    const reconstructDraftFromSections = () => {
        const sections = host.activeScene?.prompt_sections || [];
        const template = host._channelTemplate();
        const keys = templateChannelKeys(template);
        const normalizedSections = sections.map((section) => {
            const channels = normalizeChannels(section.channels, section.prompt, keys);
            const channelDocs = Object.fromEntries(keys.map((key) => [
                key, normalizePromptDocument(section.channel_docs?.[key], channels[key] || ""),
            ]));
            return { ...section, channel_docs: channelDocs };
        });
        writingState.document = joinWritingSectionDocuments(normalizedSections, keys);
        writingState.draft = promptDocumentText(writingState.document);
        writingState.attachments = normalizePromptAttachments(
            sections.flatMap((section) => section.attachments || []));
        writingState.allocations = sections.map((s) => ({
            length: Math.max(1, (s.end_frame || 0) - (s.start_frame || 0)),
            dirty: false,
        }));
        const blockDocuments = splitWritingPromptDocument(
            writingState.document, { keepEmpty: sections.length > 0 });
        writingState.blockMeta = sections.map((section, index) => {
            const anchors = new Set(Object.values(section.channel_docs || {})
                .flatMap((value) => normalizePromptDocument(value).nodes)
                .filter((node) => node.type === "attachment")
                .map((node) => node.attachment_id));
            return {
                block_id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`,
                source_prompt_id: section.prompt_id || "",
                merged_source_prompt_ids: [section.prompt_id || ""].filter(Boolean),
                node_ids: documentNodeIds(blockDocuments[index]),
                muted: section.muted === true,
                global_channel_exceptions: [...new Set(
                    (section.global_channel_exceptions || []).map(String))],
                attachments: normalizePromptAttachments(section.attachments)
                    .filter((value) => !anchors.has(value.attachment_id)),
            };
        });
        writingState.baseModifiedAt = getProjectVersion(host._projectDirName?.() || "");
    };
    const loadWritingState = () => {
        const key = writingDraftKey();
        if (writingState.key === key) return;
        writingState.key = key;
        const saved = host._settings?.prompts?.writingDraftByProjectScene?.[key];
        if (saved && (String(saved.draft || "").trim() || saved.document)) {
            const currentSectionsById = new Map(
                (host.activeScene?.prompt_sections || []).map((section) => [
                    String(section.prompt_id || ""), section,
                ]));
            writingState.document = normalizePromptDocument(
                saved.document, String(saved.draft || ""));
            writingState.draft = promptDocumentText(writingState.document);
            writingState.attachments = normalizePromptAttachments(saved.attachments);
            writingState.allocations = (saved.allocations || []).map((a) => ({
                length: Math.max(0, parseInt(a?.length, 10) || 0),
                dirty: !!a?.dirty,
            }));
            writingState.blockMeta = (saved.blockMeta || []).map((value) => ({
                block_id: String(value?.block_id || "")
                    || (globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`),
                source_prompt_id: String(value?.source_prompt_id || ""),
                merged_source_prompt_ids: Array.isArray(value?.merged_source_prompt_ids)
                    ? value.merged_source_prompt_ids.map(String).filter(Boolean)
                    : [String(value?.source_prompt_id || "")].filter(Boolean),
                node_ids: Array.isArray(value?.node_ids)
                    ? value.node_ids.map(String).filter(Boolean) : [],
                muted: Object.prototype.hasOwnProperty.call(value || {}, "muted")
                    ? value?.muted === true
                    : currentSectionsById.get(String(value?.source_prompt_id || ""))?.muted === true,
                global_channel_exceptions: [...new Set(
                    (Array.isArray(value?.global_channel_exceptions)
                        ? value.global_channel_exceptions
                        : currentSectionsById.get(String(
                            value?.source_prompt_id || ""))?.global_channel_exceptions || [])
                        .map(String))],
                attachments: normalizePromptAttachments(value?.attachments),
            }));
            writingState.baseModifiedAt = String(saved.baseModifiedAt || "");
        } else {
            reconstructDraftFromSections();
        }
    };
    const saveWritingState = () => {
        if (!writingState.key) return;
        if (writingState.draft.length > WRITING_DRAFT_TEXT_CAP) {
            notifyWarning("Writing draft exceeds the 20k browser-draft guidance.", { source: "prompt-writing-draft-cap" });
        }
        const map = { ...(host._settings?.prompts?.writingDraftByProjectScene || {}) };
        map[writingState.key] = {
            ts: Date.now(),
            draft: writingState.draft,
            document: structuredClone(writingState.document),
            attachments: structuredClone(writingState.attachments),
            baseModifiedAt: writingState.baseModifiedAt,
            blockMeta: structuredClone(writingState.blockMeta),
            allocations: writingState.allocations.map((a) => ({ length: a.length, dirty: !!a.dirty })),
        };
        host._updateSettings({ prompts: { writingDraftByProjectScene: map } });
    };
    const clearWritingState = () => {
        const map = { ...(host._settings?.prompts?.writingDraftByProjectScene || {}) };
        map[writingState.key] = { ts: Date.now(), draft: "", document: null,
            attachments: [], allocations: [], blockMeta: [], baseModifiedAt: "" };
        host._updateSettings({ prompts: { writingDraftByProjectScene: map } });
        writingState.key = ""; // force reload (reconstruct) on next render
    };
    const reconcileWritingBlockMeta = (blockDocuments) => {
        const old = writingState.blockMeta.map((value, index) => ({
            ...value, index, ids: new Set(value.node_ids || []),
        }));
        const claimed = new Set();
        const next = [];
        for (let blockIndex = 0; blockIndex < blockDocuments.length; blockIndex++) {
            const ids = new Set(documentNodeIds(blockDocuments[blockIndex]));
            const candidates = old.map((meta) => ({ meta, score: [...ids]
                .filter((id) => meta.ids.has(id)).length }))
                .filter((value) => value.score > 0)
                .sort((left, right) => right.score - left.score
                    || left.meta.index - right.meta.index);
            let meta;
            if (candidates.length && !claimed.has(candidates[0].meta.index)) {
                const matched = candidates.map((value) => value.meta)
                    .filter((value) => !claimed.has(value.index));
                const primary = matched.sort((left, right) => left.index - right.index)[0];
                matched.forEach((value) => claimed.add(value.index));
                meta = {
                    ...primary,
                    global_channel_exceptions: [...new Set(matched.flatMap(
                        (value) => value.global_channel_exceptions || []))],
                    attachments: coalesceScopedAttachments(
                        matched.flatMap((value) => value.attachments || [])),
                    merged_source_prompt_ids: [...new Set(matched.flatMap((value) =>
                        value.merged_source_prompt_ids?.length
                            ? value.merged_source_prompt_ids
                            : [value.source_prompt_id]).filter(Boolean))],
                    _writing_source_indices: matched.map((value) => value.index),
                };
            } else if (candidates.length) {
                // The same prior block now contributes to more than one block:
                // this is a split. The left claimant retains its identity;
                // later blocks get new identities and cloned applicable scope
                // chips sharing the original emission group.
                const source = candidates[0].meta;
                const clones = cloneSplitScopedAttachments(source.attachments || []);
                writingState.attachments = normalizePromptAttachments([
                    ...writingState.attachments, ...clones,
                ]);
                meta = {
                    block_id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`,
                    source_prompt_id: `draft:${globalThis.crypto?.randomUUID?.()
                        || `${Date.now()}-${Math.random()}`}`,
                    merged_source_prompt_ids: [],
                    muted: source.muted === true,
                    global_channel_exceptions: [...(source.global_channel_exceptions || [])],
                    attachments: clones,
                    _writing_source_indices: [source.index],
                };
            } else {
                // Legacy drafts predate node-id metadata. A positional match is
                // safe only while that old slot is still unclaimed.
                const positional = old[blockIndex];
                if (positional && !positional.ids.size && !claimed.has(positional.index)) {
                    claimed.add(positional.index); meta = {
                        ...positional, _writing_source_indices: [positional.index],
                    };
                } else {
                    // Text fragments to the right of a newly typed separator
                    // deliberately receive new node ids. When the block count
                    // grew, the preceding block is therefore the only stable
                    // source identity available for a text-only split.
                    const priorIndices = next.at(-1)?._writing_source_indices || [];
                    const splitSource = blockDocuments.length > old.length && priorIndices.length
                        ? old[priorIndices.at(-1)] : null;
                    const clones = splitSource
                        ? cloneSplitScopedAttachments(splitSource.attachments || []) : [];
                    if (clones.length) writingState.attachments = normalizePromptAttachments([
                        ...writingState.attachments, ...clones,
                    ]);
                    meta = {
                        block_id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`,
                        source_prompt_id: `draft:${globalThis.crypto?.randomUUID?.()
                            || `${Date.now()}-${Math.random()}`}`,
                        merged_source_prompt_ids: [],
                        muted: splitSource?.muted === true,
                        global_channel_exceptions: [...(splitSource?.global_channel_exceptions || [])],
                        attachments: clones,
                        _writing_source_indices: splitSource ? [splitSource.index] : [],
                    };
                }
            }
            delete meta.index; delete meta.ids;
            if (!meta.source_prompt_id) {
                meta.source_prompt_id = `draft:${globalThis.crypto?.randomUUID?.()
                    || `${Date.now()}-${Math.random()}`}`;
            }
            meta.node_ids = [...ids];
            next.push(meta);
        }
        next.forEach((value) => { delete value._writing_source_indices; });
        writingState.blockMeta = next;
        return blockDocuments;
    };

    // ── Header ───────────────────────────────────────────────────────
    const header = document.createElement("div");
    header.style.cssText = `
        display:flex; align-items:center; justify-content:space-between;
        padding: 12px 18px; border-bottom: 1px solid ${COLORS.promptBorder};
        font-family:${FONT.sans}; flex:0 0 auto;
    `;
    const title = document.createElement("div");
    title.style.cssText = `font-size:13px; font-weight:600; color:${COLORS.text};`;
    title.textContent = "Prompt Management";
    const closeBtn = makeBtn("Close", "Close (Esc)", "subtle");
    closeBtn.addEventListener("click", close);
    header.append(title, closeBtn);
    panel.appendChild(header);

    const diagnostics = document.createElement("div");
    diagnostics.dataset.sonderPromptDiagnostics = "1";
    diagnostics.style.cssText = `padding:7px 18px;border-bottom:1px solid ${COLORS.trackBorder};`
        + `background:${COLORS.panelMuted};font:10px/1.35 ${FONT.sans};color:${COLORS.textDim};`
        + "display:flex;flex-direction:column;gap:3px;height:72px;overflow-y:auto;box-sizing:border-box;flex:0 0 auto;";
    panel.appendChild(diagnostics);

    const body = document.createElement("div");
    body.style.cssText = `
        flex:1; min-height:0; overflow-y:auto; padding: 12px 18px; display:flex;
        flex-direction:column; gap: 14px; font-family:${FONT.sans};
    `;
    panel.appendChild(body);

    const currentCandidatePayload = () => {
        const payload = host._promptContextCandidateCache;
        return payload?._candidate_scene_id === host.activeSceneId ? payload : null;
    };
    const currentManagedSpeakerSubjectIds = () =>
        currentCandidatePayload()?.managed_speaker_subject_ids || [];
    const renderDiagnostics = (payload = currentCandidatePayload()) => {
        const state = buildPromptContextDiagnostics(payload);
        diagnostics.style.opacity = state.stale ? "0.62" : "1";
        diagnostics.dataset.sonderPromptDiagnosticsStale = state.stale ? "1" : "0";
        diagnostics.textContent = "";
        const windowLine = document.createElement("div");
        windowLine.textContent = state.windowLabel;
        windowLine.style.color = COLORS.textMuted;
        diagnostics.appendChild(windowLine);
        for (const row of state.general) {
            const line = document.createElement("div");
            const text = document.createElement("span");
            text.textContent = `${row.tier === "error" ? "Error" : "Warning"} ${row.code}: ${row.message}`;
            line.appendChild(text);
            line.style.color = row.tier === "error" ? COLORS.dangerText : COLORS.warningText;
            if (row.tier === "warning"
                    && ["overall_soundscape", "non_diegetic_music"].includes(row.channel_key)) {
                const addNA = makeBtn("Add N/A", `Write N/A into the global ${row.channel_key} channel`);
                addNA.style.marginLeft = "6px";
                addNA.addEventListener("mousedown", (event) => event.preventDefault());
                addNA.addEventListener("click", () => {
                    const selector = `[data-sonder-prompt-global-channel="${row.channel_key}"]`;
                    const input = panel.querySelector(selector);
                    if (!input) return;
                    input.focus();
                    input.insertText?.("N/A");
                    input.blur();
                });
                line.appendChild(addNA);
            }
            diagnostics.appendChild(line);
        }
        if (state.attachmentDiagnosticCount) {
            const line = document.createElement("div");
            line.textContent = `${state.attachmentDiagnosticCount} chip diagnostic${state.attachmentDiagnosticCount === 1 ? " is" : "s are"} marked below.`;
            line.style.color = state.errorCount ? COLORS.dangerText : COLORS.warningText;
            diagnostics.appendChild(line);
        } else if (!state.general.length && payload) {
            const clean = document.createElement("div");
            clean.textContent = "No compile diagnostics in this render window.";
            diagnostics.appendChild(clean);
        }
        for (const badge of panel.querySelectorAll("[data-sonder-queue-advisory-count]")) {
            badge.textContent = state.warningCount
                ? `${state.warningCount} advisor${state.warningCount === 1 ? "y" : "ies"}` : "";
            badge.style.display = state.warningCount ? "inline" : "none";
            badge.title = state.warningCount
                ? "The current effective render window has non-blocking prompt advisories." : "";
        }

        for (const projections of panel.querySelectorAll(
            "[data-sonder-prompt-channel-projections]")) {
            projections._sonderRefreshProjection?.(payload);
        }

        for (const chip of panel.querySelectorAll("[data-attachment-id]")) {
            if (!Object.prototype.hasOwnProperty.call(chip.dataset, "sonderDiagnosticBaseTitle")) {
                chip.dataset.sonderDiagnosticBaseTitle = chip.title || "Dynamic prompt context";
            }
            chip.title = chip.dataset.sonderDiagnosticBaseTitle;
            chip.style.boxShadow = "";
            chip.removeAttribute("aria-invalid");
            delete chip.dataset.sonderDiagnosticTier;
            const rows = state.byAttachment[chip.dataset.attachmentId] || [];
            if (!rows.length) continue;
            const hasError = rows.some((row) => row.tier === "error");
            chip.dataset.sonderDiagnosticTier = hasError ? "error" : "warning";
            chip.style.boxShadow = `inset 0 0 0 1px ${hasError ? COLORS.dangerText : COLORS.warningText}`;
            if (hasError) chip.setAttribute("aria-invalid", "true");
            chip.title = `${chip.title}\n\n${promptContextDiagnosticTitle(rows)}`;
        }
    };

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
        hint.textContent = `Write freely; a line containing only ${WRITING_BREAK} splits sections, and a line like "${templateChannelKeys(host._channelTemplate())[0]}:" starts that channel. Unlabelled text goes to the first channel. Apply replaces the lane's sections (undoable).`;
        bodyEl.appendChild(hint);

        let draftTimer = null;
        let draftArea = null;
        const draftAttachmentContext = (nodeId = "") => {
            const blockDocuments = reconcileWritingBlockMeta(
                splitWritingPromptDocument(writingState.document, {
                    keepEmpty: writingState.blockMeta.length > 0,
                }));
            const selectedNodeId = nodeId || draftArea?.promptSelection?.node_id || "";
            let selectedIndex = blockDocuments.findIndex((documentValue) =>
                documentNodeIds(documentValue).includes(selectedNodeId));
            if (selectedIndex < 0) selectedIndex = 0;
            let cursor = 0;
            const promptSections = blockDocuments.map((documentValue, index) => {
                const length = Math.max(
                    minLen, writingState.allocations[index]?.length ?? minLen);
                const value = {
                    prompt_id: writingState.blockMeta[index]?.source_prompt_id,
                    start_frame: cursor,
                    end_frame: cursor + length,
                    prompt: promptDocumentText(documentValue),
                };
                cursor += length;
                return value;
            });
            const selected = promptSections[selectedIndex] || {
                start_frame: 0, end_frame: minLen,
            };
            return {
                scene: { ...(scene || {}), prompt_sections: promptSections,
                    _context_channel_keys: templateChannelKeys(host._channelTemplate()),
                    _context_consumer_start: selected.start_frame,
                    _context_consumer_end: selected.end_frame,
                    _context_reference_frame_threshold: host._referenceFrameThreshold || 0 },
                blockDocuments,
                selectedIndex,
            };
        };
        const configureDraftAttachment = (attachment, nodeId = "") => {
            const context = draftAttachmentContext(nodeId);
            return configurePromptAttachment(attachment, {
                scene: context.scene,
                references: host._references || [],
                semanticUnits: host._promptSemanticUnits || [],
                profileId: scene?.prompt_context_profile_id
                    || host._channelTemplate().default_context_profile || "generic@1",
                templateId: host._channelTemplate().id || "",
                scope: "section",
                anchoredChannels: [context.scene?._context_channel_keys?.[0]].filter(Boolean),
                taskTypes: host._promptContextCatalog?.minimax_task_types || [],
                managedSpeakerSubjectIds: currentManagedSpeakerSubjectIds(),
                ordinalManifest: currentCandidatePayload()?.ordinal_manifest || {},
                candidate: currentCandidatePayload(),
            });
        };
        draftArea = createPromptDocumentEditor({
            document: writingState.document,
            text: writingState.draft,
            attachments: writingState.attachments,
            previews: currentCandidatePayload()?.attachment_previews || {},
            attachmentLabelFor,
            attachmentContext: { scene, template: host._channelTemplate() },
            disabled: applyBlocked,
            ariaLabel: "Writing mode narrative draft",
            onChange: ({ document: nextDocument, attachments, text, reason }) => {
                writingState.document = nextDocument;
                writingState.attachments = attachments;
                writingState.draft = text;
                if (["attachment", "history"].includes(reason)) saveWritingState();
                if (draftTimer) clearTimeout(draftTimer);
                draftTimer = setTimeout(() => updateStrip(), 300);
            },
            onActivateAttachment: async (attachment, node) => {
                const configured = await configureDraftAttachment(
                    attachment, String(node?.node_id || ""));
                if (configured) draftArea.replaceAttachment(configured);
            },
            getHostSnapshot: () => writingState.blockMeta,
            onRestoreHostSnapshot: (value) => {
                writingState.blockMeta = Array.isArray(value) ? value : [];
            },
        });
        draftArea.style.minHeight = "120px";
        applyBoxHeight(host, draftArea, "panelDraftBoxHeight", 160);
        registerPromptBoxGuard(draftArea, () => writingState.draft);
        draftArea.addEventListener("blur", (event) => {
            if (guard.suppressBlurCommit) return;
            if (event.relatedTarget?.closest?.("[data-sonder-prompt-context-modal='1'],[data-sonder-prompt-context-menu='1']")) return;
            writingState.draft = draftArea.value;
            writingState.document = draftArea.promptDocument;
            writingState.attachments = draftArea.promptAttachments;
            saveWritingState();
        });
        draftArea.addEventListener("keydown", (e) => e.stopPropagation());
        installPromptContextMenu({
            editor: draftArea,
            onInserted: () => {
                writingState.draft = draftArea.value;
                writingState.document = draftArea.promptDocument;
                writingState.attachments = draftArea.promptAttachments;
                saveWritingState();
            },
            onCreate: (attachment) => configureDraftAttachment(attachment),
            writingAids: host._promptContextWritingAids?.(
                scene?.prompt_context_profile_id
                    || host._channelTemplate().default_context_profile || "generic@1") || [],
        });
        bodyEl.appendChild(draftArea);
        bodyEl.appendChild(makeHeightGrip(host, bodyEl, "panelDraftBoxHeight", { min: 120, label: "Drag to resize the draft box (persists)" }));
        appendPromptContextHint(host, bodyEl);

        const toolRow = document.createElement("div");
        toolRow.style.cssText = "display:flex; gap:6px; align-items:center; flex-wrap:wrap;";
        const splitBtn = makeBtn("Split here", "Insert a --- section break at the cursor");
        splitBtn.addEventListener("click", () => {
            draftArea.focus();
            document.execCommand?.("insertText", false, `\n${WRITING_BREAK}\n`);
            writingState.document = draftArea.promptDocument;
            writingState.draft = draftArea.value;
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

        const previewWritingBlocks = (blockDocuments) => {
            const attachmentById = new Map(writingState.attachments
                .map((attachment) => [attachment.attachment_id, attachment]));
            const channelKeys = templateChannelKeys(host._channelTemplate());
            let cursor = 0;
            const promptSections = blockDocuments.map((blockDocument, index) => {
                const length = Math.max(
                    minLen, writingState.allocations[index]?.length ?? minLen);
                const channelDocs = splitPromptDocumentChannels(blockDocument, channelKeys);
                const anchorIds = new Set(Object.values(channelDocs)
                    .flatMap((documentValue) => documentValue.nodes)
                    .filter((node) => node.type === "attachment")
                    .map((node) => node.attachment_id));
                const attachments = coalesceScopedAttachments([
                    ...normalizePromptAttachments(writingState.blockMeta[index]?.attachments)
                        .filter((value) => !anchorIds.has(value.attachment_id)),
                    ...[...anchorIds].map((id) => attachmentById.get(id)).filter(Boolean),
                ]);
                const value = {
                    prompt_id: writingState.blockMeta[index]?.source_prompt_id
                        || `preview:${index}`,
                    start_frame: cursor,
                    end_frame: cursor + length,
                    channels: Object.fromEntries(channelKeys.map((key) => [
                        key, promptDocumentText(channelDocs[key]).trim(),
                    ])),
                    channel_docs: channelDocs,
                    attachments,
                    muted: writingState.blockMeta[index]?.muted === true,
                    global_channel_exceptions: [...new Set(
                        (writingState.blockMeta[index]?.global_channel_exceptions || [])
                            .map(String))],
                };
                cursor += length;
                return value;
            });
            host._previewPromptContextCandidate?.({
                prompt_sections: promptSections,
                duration_frames: Math.max(totalFrames, cursor),
            });
        };

        const updateReadout = (blocks) => {
            const total = chipLengths();
            const requiredTotal = laidOutTotal(blocks);
            const willExtend = blocks.length > 0 && requiredTotal > totalFrames;
            readout.textContent = blocks.length
                ? `Sections: ${blocks.length} — total ${total}f of ${totalFrames}f (${Math.max(0, totalFrames - total)}f remaining; min ${minLen}f per section)`
                : "No sections yet — the whole draft is one block until you add --- lines.";
            if (willExtend) readout.textContent += ` — Apply will extend the scene to ${requiredTotal}f.`;
            const currentVersion = getProjectVersion(host._projectDirName?.() || "");
            const stale = !!(writingState.baseModifiedAt && currentVersion
                && writingState.baseModifiedAt !== currentVersion);
            if (stale) readout.textContent += " — Draft is stale; reset from sections before Apply.";
            readout.style.color = COLORS.textDim;
            // Over-budget no longer blocks — Apply extends the scene instead
            // (which also satisfies the per-section minimum). Only a locked
            // lane or an empty draft can block.
            if (applyBlocked) applyBtn.textContent = "Apply (locked)";
            else applyBtn.textContent = willExtend ? `Apply & Extend Scene to ${requiredTotal}f` : "Apply";
            applyBtn.disabled = applyBlocked || stale || !blocks.length;
        };

        const updateStrip = () => {
            const blockDocuments = reconcileWritingBlockMeta(
                splitWritingPromptDocument(writingState.document, {
                    keepEmpty: writingState.blockMeta.length > 0,
                }));
            const blocks = blockDocuments.map(promptDocumentText);
            if (blocks.length !== writingState.allocations.length) {
                writingState.allocations = allocateWritingBlocks(
                    blocks, totalFrames, minLen, writingState.allocations);
            }
            strip.textContent = "";
            blocks.forEach((text, i) => {
                const blockStart = writingState.allocations.slice(0, i)
                    .reduce((sum, value) => sum + Math.max(minLen, value?.length ?? minLen), 0);
                const blockEnd = blockStart
                    + Math.max(minLen, writingState.allocations[i]?.length ?? minLen);
                const attachmentScene = { ...(scene || {}),
                    _context_channel_keys: templateChannelKeys(host._channelTemplate()),
                    _context_consumer_start: blockStart,
                    _context_consumer_end: blockEnd,
                    _context_reference_frame_threshold: host._referenceFrameThreshold || 0 };
                const blockCard = document.createElement("div");
                blockCard.style.cssText = "display:flex;flex-direction:column;gap:3px;";
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
                    previewWritingBlocks(blockDocuments);
                });
                lengthInput.addEventListener("keydown", (e) => e.stopPropagation());
                syncHint();
                chip.append(preview, lengthInput, timecodeHint);
                blockCard.appendChild(chip);
                const anchorIds = new Set(normalizePromptDocument(blockDocuments[i]).nodes
                    .filter((node) => node.type === "attachment")
                    .map((node) => node.attachment_id));
                const renderBlockScope = () => {
                    const meta = writingState.blockMeta[i];
                    const scoped = normalizePromptAttachments(meta?.attachments)
                        .filter((value) => !anchorIds.has(value.attachment_id));
                    const row = createScopeChipRow({
                        attachments: scoped,
                        previews: currentCandidatePayload()?.attachment_previews || {},
                        attachmentLabelFor,
                        disabled: applyBlocked,
                        allowedKinds: host._channelTemplate().shot_marker_channel
                            ? ["shot", "timestamp", "reference", "custom"]
                            : ["reference", "custom"],
                        reusableAttachments: writingState.attachments,
                        allSceneAttachments: writingState.attachments,
                        reuseContext: { scene: attachmentScene,
                            template: host._channelTemplate() },
                        onReuse: (configured) => {
                            const nextRegistry = normalizePromptAttachments([
                                ...writingState.attachments, configured]);
                            draftArea.transactPromptAttachments?.(nextRegistry, () => {
                                meta.attachments = [...scoped, configured];
                            });
                            saveWritingState(); renderBlockScope();
                            previewWritingBlocks(blockDocuments);
                        },
                        onAdd: async (kind) => {
                            const configured = await configurePromptAttachment({ kind }, {
                                scene: attachmentScene,
                                references: host._references || [],
                                semanticUnits: host._promptSemanticUnits || [],
                                profileId: scene?.prompt_context_profile_id
                                    || host._channelTemplate().default_context_profile || "generic@1",
                                templateId: host._channelTemplate().id || "",
                                scope: "section",
                                anchoredChannels: [],
                                taskTypes: host._promptContextCatalog?.minimax_task_types || [],
                                managedSpeakerSubjectIds: currentManagedSpeakerSubjectIds(),
                                ordinalManifest: currentCandidatePayload()?.ordinal_manifest || {},
                                candidate: currentCandidatePayload(),
                            });
                            if (!configured) return;
                            const nextRegistry = normalizePromptAttachments([
                                ...writingState.attachments, configured]);
                            draftArea.transactPromptAttachments?.(nextRegistry, () => {
                                meta.attachments = [...scoped, configured];
                            });
                            saveWritingState(); renderBlockScope();
                            previewWritingBlocks(blockDocuments);
                        },
                        onActivate: async (attachment) => {
                            const configured = await configurePromptAttachment(attachment, {
                                scene: attachmentScene,
                                references: host._references || [],
                                semanticUnits: host._promptSemanticUnits || [],
                                profileId: scene?.prompt_context_profile_id
                                    || host._channelTemplate().default_context_profile || "generic@1",
                                templateId: host._channelTemplate().id || "",
                                scope: "section",
                                anchoredChannels: [],
                                taskTypes: host._promptContextCatalog?.minimax_task_types || [],
                                managedSpeakerSubjectIds: currentManagedSpeakerSubjectIds(),
                                candidate: currentCandidatePayload(),
                                ordinalManifest: currentCandidatePayload()?.ordinal_manifest || {},
                            });
                            if (!configured) return;
                            const groupId = attachment.emission_group_id;
                            const linked = writingState.attachments.filter((value) =>
                                value.emission_group_id === groupId).length > 1;
                            const nextRegistry = writingState.attachments.map((value) =>
                                linked && value.emission_group_id === groupId
                                    ? propagateLinkedPromptAttachment(
                                        configured, value, attachment.attachment_id)
                                    : (value.attachment_id === configured.attachment_id
                                        ? configured : value));
                            draftArea.transactPromptAttachments?.(nextRegistry, () => {
                                for (const blockMeta of writingState.blockMeta) {
                                    blockMeta.attachments = normalizePromptAttachments(
                                        blockMeta.attachments).map((value) =>
                                        linked && value.emission_group_id === groupId
                                            ? propagateLinkedPromptAttachment(
                                                configured, value, attachment.attachment_id)
                                            : (value.attachment_id === configured.attachment_id
                                                ? configured : value));
                                }
                            });
                            saveWritingState(); renderBlockScope();
                            previewWritingBlocks(blockDocuments);
                        },
                        onUnlink: (configured) => {
                            const nextRegistry = writingState.attachments.map((value) =>
                                value.attachment_id === configured.attachment_id
                                    ? configured : value);
                            draftArea.transactPromptAttachments?.(nextRegistry, () => {
                                meta.attachments = scoped.map((value) =>
                                    value.attachment_id === configured.attachment_id
                                        ? configured : value);
                            });
                            saveWritingState(); renderBlockScope();
                            previewWritingBlocks(blockDocuments);
                        },
                        onRemove: (attachment) => {
                            const nextRegistry = writingState.attachments.filter((value) =>
                                value.attachment_id !== attachment.attachment_id);
                            draftArea.transactPromptAttachments?.(nextRegistry, () => {
                                meta.attachments = scoped.filter((value) =>
                                    value.attachment_id !== attachment.attachment_id);
                            });
                            saveWritingState(); renderBlockScope();
                            previewWritingBlocks(blockDocuments);
                        },
                    });
                    const previous = blockCard.querySelector("[data-writing-scope]");
                    row.dataset.writingScope = "1";
                    if (previous) previous.replaceWith(row); else blockCard.appendChild(row);
                };
                renderBlockScope();
                strip.appendChild(blockCard);
            });
            updateReadout(blocks);
            previewWritingBlocks(blockDocuments);
        };

        equalizeBtn.addEventListener("click", () => {
            const blocks = splitWritingPromptDocument(writingState.document, {
                keepEmpty: writingState.blockMeta.length > 0,
            })
                .map(promptDocumentText);
            writingState.allocations = allocateWritingBlocks(
                blocks.map(() => " "), totalFrames, minLen, []); // equal weights
            saveWritingState();
            updateStrip();
        });

        applyBtn.addEventListener("click", async () => {
            if (applyBtn.disabled) return;
            const blocks = reconcileWritingBlockMeta(
                splitWritingPromptDocument(writingState.document, {
                    keepEmpty: writingState.blockMeta.length > 0,
                }));
            const attachmentById = new Map(writingState.attachments
                .map((attachment) => [attachment.attachment_id, attachment]));
            const channelKeys = templateChannelKeys(host._channelTemplate());
            let cursor = 0;
            const sections = blocks.map((blockDocument, i) => {
                const length = Math.max(minLen, writingState.allocations[i]?.length ?? minLen);
                const channelDocs = splitPromptDocumentChannels(blockDocument, channelKeys);
                const channels = Object.fromEntries(channelKeys.map((key) => [
                    key, promptDocumentText(channelDocs[key]).trim(),
                ]));
                const anchorIds = new Set(Object.values(channelDocs)
                    .flatMap((documentValue) => documentValue.nodes)
                    .filter((node) => node.type === "attachment")
                    .map((node) => node.attachment_id));
                const scoped = normalizePromptAttachments(
                    writingState.blockMeta[i]?.attachments)
                    .filter((value) => !anchorIds.has(value.attachment_id));
                const attachments = coalesceScopedAttachments([
                    ...scoped,
                    ...[...anchorIds].map((id) => attachmentById.get(id)).filter(Boolean),
                ]);
                const section = {
                    prompt_id: writingState.blockMeta[i]?.source_prompt_id
                        || (globalThis.crypto?.randomUUID?.() || `${Date.now()}-${i}`),
                    start_frame: cursor,
                    end_frame: cursor + length,
                    channels,
                    channel_docs: channelDocs,
                    attachments,
                    muted: writingState.blockMeta[i]?.muted === true,
                    global_channel_exceptions: [...new Set(
                        (writingState.blockMeta[i]?.global_channel_exceptions || [])
                            .map(String))],
                };
                cursor += length;
                return section;
            });
            const promptIdRemap = new Map();
            sections.forEach((section, index) => {
                for (const oldId of writingState.blockMeta[index]?.merged_source_prompt_ids || []) {
                    if (oldId) promptIdRemap.set(oldId, section.prompt_id);
                }
            });
            for (const section of sections) {
                section.attachments = section.attachments.map((attachment) => {
                    if (attachment.kind !== "prompt_link") return attachment;
                    const sourceId = String(attachment.source?.prompt_id || "");
                    const rebound = promptIdRemap.get(sourceId);
                    return rebound ? { ...attachment,
                        source: { ...attachment.source, prompt_id: rebound } } : attachment;
                });
            }
            const sceneDur = host.activeScene?.duration_frames || host.totalFrames || 0;
            const extendDurationTo = cursor > sceneDur ? cursor : 0;
            // The writing tool rewrites SECTIONS only, so the scene-global text
            // has to be handed back untouched. Passing just `global` is not that:
            // it is the label-free mirror over the LEGACY three channels, so it
            // reads empty on any other template, and the flat field is
            // destructive server-side (`Scene.set_global_prompt` clears channels
            // 2..n). Apply would silently erase the whole global bag. Carry the
            // channels themselves, tagged with the template they were authored
            // under so no retarget is attempted.
            try {
                await host._applyPromptSetup({
                    global: host.activeScene?.prompt || "",
                    global_channels: { ...(host.activeScene?.global_channels || {}) },
                    global_channel_docs: structuredClone(host.activeScene?.global_channel_docs || {}),
                    global_attachments: structuredClone(host.activeScene?.global_attachments || []),
                    source_channel_template_id: host._channelTemplate().id,
                    source_channel_template: host._channelTemplate(),
                    sections,
                    extendDurationTo,
                    base_modified_at: writingState.baseModifiedAt,
                });
            } catch (error) {
                notifyWarning(error?.message || "Writing draft Apply was refused.",
                    { source: "prompt-writing-stale" });
                return;
            }
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

    const renderContextSettings = (bodyEl, scene) => {
        bodyEl.appendChild(sectionTitle("Prompt Context"));
        const card = document.createElement("div");
        card.style.cssText = `display:flex;flex-direction:column;gap:7px;padding:8px;border:1px solid ${COLORS.promptBorder};border-radius:6px;background:#181d25;`;
        const profile = document.createElement("select");
        profile.style.cssText = chromeInputCss();
        const activeTemplate = host._channelTemplate();
        const defaultProfile = activeTemplate.default_context_profile || "generic@1";
        // Provider requirements are enforced per channel template, so pairing a
        // format with a template it does not target would let the format claim
        // requirements nothing checks. Incompatible choices are shown disabled
        // rather than hidden, so an existing bad pairing stays visible.
        const catalog = host._promptContextCatalog || {};
        const catalogReady = Number(catalog.schema_version) === 1
            && Array.isArray(catalog.profiles);
        const catalogProfiles = catalogReady ? catalog.profiles : [];
        const universal = String(catalog.universal_template || "*");
        const activeTemplateId = String(activeTemplate.id || "");
        const catalogByKey = new Map(catalogProfiles.map(
            (value) => [String(value?.key || ""), value]));
        const compatible = (key) => {
            if (!key) return true;
            // A template naming a format as its own default has declared the
            // pairing — mirror `profile_matches_template` exactly, or a copied
            // MiniMax template shows its own default disabled while the server
            // happily compiles it.
            if (key === defaultProfile) return true;
            const allowed = catalogByKey.get(key)?.compatible_templates;
            if (!Array.isArray(allowed) || !allowed.length) return false;
            return allowed.includes(universal) || allowed.includes("standard")
                || allowed.includes(activeTemplateId);
        };
        const addProfileOption = (value, label) => {
            const option = document.createElement("option");
            option.value = value;
            option.textContent = compatible(value) ? label
                : `${label} — not available for this channel template`;
            option.disabled = !compatible(value);
            profile.appendChild(option);
            return option;
        };
        addProfileOption("", `Template default (${defaultProfile})`);
        for (const value of catalogProfiles) {
            const key = String(value?.key || "");
            if (key) addProfileOption(key, value.name || key);
        }
        const selectedProfileKey = String(scene.prompt_context_profile_id || "");
        if (selectedProfileKey && !catalogByKey.has(selectedProfileKey)) {
            addProfileOption(selectedProfileKey,
                `${selectedProfileKey} - unavailable from catalog`);
        }
        profile.value = selectedProfileKey;
        profile.disabled = !catalogReady;
        const profileRow = document.createElement("label");
        profileRow.style.cssText = `display:flex;align-items:center;gap:8px;font-size:10px;color:${COLORS.textDim};`;
        profileRow.append("Prompt format", profile); card.appendChild(profileRow);
        if (!catalogReady) {
            const state = document.createElement("div");
            state.style.cssText = `font-size:10px;color:${host._referencesError ? "#e08b6a" : COLORS.textDim};`;
            state.textContent = host._referencesError
                ? `Prompt format catalog failed to load: ${host._referencesError}`
                : "Loading the authoritative prompt format catalog...";
            card.appendChild(state);
        }
        const fork = makeBtn("Fork prompt format", "Create a bounded declarative project prompt format");
        fork.disabled = !catalogReady;
        fork.addEventListener("click", () => {
            if (card.querySelector("[data-profile-editor]")) return;
            const selectedKey = profile.value || defaultProfile;
            const descriptor = catalogByKey.get(selectedKey);
            if (!descriptor?.fork_seed) return;
            const selected = structuredClone(descriptor.fork_seed);
            const editor = document.createElement("div"); editor.dataset.profileEditor = "1";
            editor.style.cssText = "display:grid;grid-template-columns:110px minmax(0,1fr);gap:5px;align-items:center;font-size:10px;";
            const addField = (label, value = "") => {
                const title = document.createElement("span"); title.textContent = label;
                const input = document.createElement("input"); input.value = value;
                input.style.cssText = chromeInputCss(); editor.append(title, input); return input;
            };
            const name = addField("Name", `Custom ${host._channelTemplate().name || "Prompt"}`);
            const id = addField("Stable ID", `custom_${Date.now().toString(36)}`);
            const version = addField("Version", "1");
            const formatter = addField("Custom-chip format", "{text}");
            const route = addField("Default channel", templateChannelKeys(host._channelTemplate())[0] || "visual");
            const writingAids = structuredClone(selected.writing_aids || []);
            const aidEditor = document.createElement("div");
            aidEditor.style.cssText = "grid-column:1/-1;display:flex;flex-direction:column;gap:4px;padding:5px;border:1px solid #343d4b;border-radius:5px;";
            const renderWritingAids = () => {
                aidEditor.replaceChildren();
                const title = document.createElement("strong");
                title.textContent = `Writing aids preserved (${writingAids.length})`;
                aidEditor.appendChild(title);
                writingAids.forEach((aid, index) => {
                    const row = document.createElement("div");
                    row.style.cssText = "display:grid;grid-template-columns:120px minmax(0,1fr) auto;gap:4px;";
                    const labelInput = document.createElement("input");
                    labelInput.value = aid.label || aid.id || "";
                    labelInput.style.cssText = chromeInputCss();
                    labelInput.setAttribute("aria-label", "Writing aid label");
                    const textInput = document.createElement("input");
                    textInput.value = aid.text || "";
                    textInput.style.cssText = chromeInputCss();
                    textInput.setAttribute("aria-label", "Writing aid text");
                    labelInput.addEventListener("input", () => { writingAids[index].label = labelInput.value; });
                    textInput.addEventListener("input", () => { writingAids[index].text = textInput.value; });
                    const remove = makeBtn("Remove", "Remove this writing aid from the fork", "danger");
                    remove.addEventListener("click", () => {
                        writingAids.splice(index, 1); renderWritingAids();
                    });
                    row.append(labelInput, textInput, remove); aidEditor.appendChild(row);
                });
            };
            renderWritingAids();
            editor.appendChild(aidEditor);
            const aidLabel = addField("New aid label", "");
            const aidText = addField("New aid text", "");
            const aidChoices = addField("New aid choices", "");
            aidChoices.placeholder = "language=English|Spanish; motion=pans left|pans right";
            const addAid = makeBtn("Add writing aid", "Add this bounded writing aid to the fork");
            addAid.addEventListener("click", () => {
                if (!aidLabel.value.trim() || !aidText.value) return;
                const declaredChoices = new Map(aidChoices.value.split(";")
                    .map((entry) => entry.split("="))
                    .filter((parts) => parts.length === 2)
                    .map(([field, values]) => [field.trim(), values.split("|")
                        .map((value) => value.trim()).filter(Boolean)]));
                const fields = {};
                for (const match of aidText.value.matchAll(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g)) {
                    if (match[1] !== "text") fields[match[1]] = {
                        type: "enum", values: declaredChoices.get(match[1]) || [],
                    };
                }
                writingAids.push({ id: `custom_${Date.now().toString(36)}`,
                    label: aidLabel.value.trim(), text: aidText.value, fields });
                aidLabel.value = ""; aidText.value = ""; aidChoices.value = "";
                renderWritingAids();
            });
            editor.append(document.createElement("span"), addAid);
            const requiredChannels = addField("Required channels", "");
            requiredChannels.placeholder = "visual,sound";
            const maxChars = addField("Max prompt chars", "");
            maxChars.type = "number"; maxChars.min = "1"; maxChars.max = "262144";
            const roleValues = (population) => (selected.role_catalogs?.[population] || [])
                .map((value) => typeof value === "string" ? value : value?.value).filter(Boolean).join(", ");
            const pictureRoles = addField("Picture roles", roleValues("pictures"));
            const videoRoles = addField("Video roles", roleValues("videos"));
            const audioRoles = addField("Audio roles", roleValues("standalone_audios"));
            for (const input of [pictureRoles, videoRoles, audioRoles]) {
                input.placeholder = "identity, style, motion";
                input.title = "Bounded per-member role choices exposed to compatible Reference recipes.";
            }
            const save = makeBtn("Save prompt format", "Validate and save this immutable project prompt format version", "primary");
            save.addEventListener("click", async () => {
                const profileId = id.value.trim().replace(/[^A-Za-z0-9_.-]+/g, "_");
                if (!profileId || !version.value.trim()) return;
                const capabilities = structuredClone(selected.capabilities || {});
                capabilities.custom = { channel_key: route.value.trim(), placement: "inline",
                    formatter: formatter.value };
                const validators = structuredClone(selected.validators || []);
                for (const channelKey of requiredChannels.value.split(",")
                    .map((value) => value.trim()).filter(Boolean)) {
                    validators.push({ kind: "required_channel", channel_key: channelKey,
                        severity: "error" });
                }
                if (maxChars.value) validators.push({ kind: "max_final_chars",
                    limit: Number(maxChars.value), severity: "error" });
                const role_catalogs = {};
                for (const [population, input] of [["pictures", pictureRoles],
                    ["videos", videoRoles], ["standalone_audios", audioRoles]]) {
                    const values = [...new Set(input.value.split(",")
                        .map((value) => value.trim()).filter(Boolean))];
                    if (values.length) role_catalogs[population] = values;
                }
                // Inherit the SOURCE format's template binding, not whichever
                // template happened to be active while forking. Writing the
                // active id would silently lock a fork made under, say, Visual +
                // Speech + Sound to that one template forever; a fork of a
                // MiniMax format must still stay bound to its own template.
                const sourceTemplates = descriptor.compatible_templates
                    || (Array.isArray(selected.compatible_templates)
                        ? selected.compatible_templates : null)
                    || [String(selected.template_id || "standard")];
                const universalToken = String(
                    host._promptContextCatalog?.universal_template || "*");
                const forkBinding = sourceTemplates.includes(universalToken)
                    ? { template_id: "standard" }
                    : { template_id: String(sourceTemplates[0] || "standard"),
                        ...(sourceTemplates.length > 1
                            ? { compatible_templates: [...sourceTemplates] } : {}) };
                const definition = { ...forkBinding,
                    capabilities, writing_aids: structuredClone(writingAids),
                    separators: structuredClone(selected.separators || { attachment: " ", line: "\n" }),
                    validators, role_catalogs };
                const collision = (host._promptContextProfiles || []).find(
                    (value) => value.profile_id === profileId
                        && String(value.version) === String(version.value.trim()));
                if (collision) {
                    notifyWarning("That prompt format version already exists. Choose a new version or stable ID.",
                        { source: "prompt-profile-immutable" });
                    return;
                }
                try {
                    await host._createPromptContextProfile?.({
                        profile_id: profileId,
                        version: version.value.trim(),
                        name: name.value.trim() || profileId,
                        definition,
                    });
                    await commit([{ type: "update_scene_fields", fields: {
                        prompt_context_profile_id: `${profileId}@${version.value.trim()}`,
                    } }], "select custom prompt profile");
                } catch (error) {
                    notifyWarning(error?.message || "Custom profile was rejected.",
                        { source: "prompt-profile-invalid" });
                }
            });
            editor.append(document.createElement("span"), save); card.appendChild(editor);
        });
        card.appendChild(fork);
        const activeSubjectScene = host.activeScene || scene;
        const subjectCandidate = currentCandidatePayload();
        const subjectWindowStart = Number(subjectCandidate?.window?.start_frame ?? 0);
        const subjectWindowEnd = Number(subjectCandidate?.window?.end_frame
            ?? activeSubjectScene?.duration_frames ?? 0);
        const subjectItems = activeSubjectScene?.reference_items || [];
        const subjectRecipes = activeSubjectScene?.reference_lane_recipes || [];
        const subjectVerdicts = resolveReferenceVerdicts({
            referenceItems: subjectItems,
            laneCount: activeSubjectScene?.reference_lane_count || 1,
            sceneDuration: activeSubjectScene?.duration_frames || 0,
            windowStart: subjectWindowStart,
            windowEnd: subjectWindowEnd,
            laneConfigs: activeSubjectScene?.reference_lane_configs || [],
            frameThresholdPct: Number(host._referenceFrameThreshold || 0),
        }).verdicts;
        const subjectSetup = (activeSubjectScene?.minimax_h3_conditioning_setups || [])
            .find((value) => value?.setup_id
                === activeSubjectScene?.active_minimax_h3_setup_id);
        const subjectSetupPopulations = new Map();
        for (const [key, population] of [["picture_lane_ids", "picture"],
            ["video_lane_ids", "video"], ["audio_lane_ids", "audio"]]) {
            for (const laneId of subjectSetup?.[key] || []) {
                subjectSetupPopulations.set(String(laneId), population);
            }
        }
        const subjectProfileId = activeSubjectScene?.prompt_context_profile_id
            || host._channelTemplate().default_context_profile || "generic@1";
        const subjectEligibility = (unit) => subjectSourceEligibility({
            sources: unit?.source_members || [], referenceItems: subjectItems,
            laneRecipes: subjectRecipes, verdicts: subjectVerdicts,
            profileId: subjectProfileId, setupPopulations: subjectSetupPopulations,
            isMiniMax: String(subjectProfileId).startsWith("minimax_h3_"),
            scope: { globalScope: false, hasSelection: true },
        });
        const subjectList = document.createElement("div");
        subjectList.dataset.sonderSubjectList = "1";
        subjectList.style.cssText = "display:flex;flex-direction:column;gap:4px;padding-top:5px;border-top:1px solid #343d4b;";
        const activeSubjectHeading = document.createElement("div");
        activeSubjectHeading.textContent = "Subjects in this setup and window";
        activeSubjectHeading.style.cssText = `font:600 10px system-ui;color:${COLORS.text};`;
        const activeSubjectRows = document.createElement("div");
        activeSubjectRows.style.cssText = "display:flex;flex-direction:column;gap:4px;";
        const otherSubjectDetails = document.createElement("details");
        const otherSubjectSummary = document.createElement("summary");
        otherSubjectSummary.textContent = "Other project Subjects";
        otherSubjectSummary.style.cssText = `font:600 10px system-ui;color:${COLORS.textDim};cursor:pointer;`;
        const otherSubjectRows = document.createElement("div");
        otherSubjectRows.style.cssText = "display:flex;flex-direction:column;gap:4px;padding-top:4px;";
        otherSubjectDetails.append(otherSubjectSummary, otherSubjectRows);
        subjectList.append(activeSubjectHeading, activeSubjectRows, otherSubjectDetails);
        for (const unit of host._promptSemanticUnits || []) {
            const eligibility = subjectEligibility(unit);
            const subjectRow = document.createElement("div");
            subjectRow.dataset.semanticUnitId = unit.semantic_unit_id;
            subjectRow.style.cssText = "display:grid;grid-template-columns:minmax(0,1fr) auto auto auto;gap:6px;align-items:center;padding:5px 6px;border:1px solid #343d4b;border-radius:5px;";
            const subjectName = document.createElement("span");
            const ownerName = unit.generated_reference_name || "its owning Reference";
            subjectName.textContent = unit.generated
                ? `${unit.name || unit.semantic_unit_id} — generated by Reference "${ownerName}"`
                : (unit.name || unit.semantic_unit_id);
            subjectName.style.cssText = `font:11px system-ui;color:${COLORS.text};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`;
            const badge = document.createElement("span");
            badge.textContent = unit.generated ? "Reference-generated" : "authored";
            badge.style.cssText = `font:8px system-ui;color:${unit.generated ? "#9fb9d8" : "#b8a9ef"};text-transform:uppercase;`;
            const edit = makeBtn("Edit", `Edit ${unit.name || "Subject"}`);
            edit.dataset.editSubject = unit.semantic_unit_id;
            const removeRow = makeBtn("Delete", "Delete this Subject", "danger");
            removeRow.dataset.deleteSubject = unit.semantic_unit_id;
            if (unit.generated) {
                removeRow.disabled = true;
                removeRow.textContent = "Managed by Reference";
                removeRow.style.opacity = "0.45";
                removeRow.title = `Generated by Reference “${ownerName}”. Remove that Reference to remove this Subject.`;
            }
            subjectRow.append(subjectName, badge, edit, removeRow);
            if (eligibility.eligible) {
                activeSubjectRows.appendChild(subjectRow);
            } else {
                const reason = document.createElement("span");
                reason.textContent = eligibility.reason || "not available in this window";
                reason.style.cssText = `grid-column:1/-1;font:9px system-ui;color:${COLORS.textDim};`;
                subjectRow.appendChild(reason);
                otherSubjectRows.appendChild(subjectRow);
            }
        }
        if (!activeSubjectRows.childElementCount) {
            const emptySubjects = document.createElement("div");
            emptySubjects.textContent = "No Subjects apply to this setup and window.";
            emptySubjects.style.cssText = `font:10px system-ui;color:${COLORS.textDim};`;
            activeSubjectRows.appendChild(emptySubjects);
        }
        if (!otherSubjectRows.childElementCount) otherSubjectDetails.style.display = "none";
        card.appendChild(subjectList);
        const deleteAuthoredSubject = async (unitId) => {
            const current = (host._promptSemanticUnits || []).find((value) =>
                value.semantic_unit_id === unitId);
            if (!current || current.generated) return false;
            const dependents = referenceBackedSubjectDependents(
                current.semantic_unit_id,
                (host.scenes || []).length ? host.scenes : [host.activeScene].filter(Boolean));
            const details = [
                [dependents.reference_chips, "Reference chip"],
                [dependents.vocal_events, "Vocal Event"],
                [dependents.audio_speaker_bindings, "audio speaker binding"],
            ].filter(([count]) => count).map(([count, label]) =>
                `${count} ${label}${count === 1 ? "" : "s"}`);
            const dependencyLine = details.length
                ? `Dependents: ${details.join(", ")}. They will remain visible as broken links so you can repair them.`
                : "No dependent chips, Vocal Events, or audio speaker bindings were found.";
            if (!globalThis.confirm?.(
                `Delete authored Subject "${current.name || current.semantic_unit_id}"?\n\n${dependencyLine}\n\nDeleting a Subject may renumber late-bound subject ordinals in compiled prompts.`)) {
                return false;
            }
            try {
                await host._savePromptSemanticUnits?.(
                    (host._promptSemanticUnits || []).filter((value) =>
                        value.semantic_unit_id !== current.semantic_unit_id));
                render();
                return true;
            } catch (error) {
                notifyWarning(error?.message || "Subject deletion was rejected.",
                    { source: "prompt-unit-delete" });
                return false;
            }
        };
        const unitsButton = makeBtn("New authored Subject",
            "Create a project-scoped Subject backed by Reference-library members");
        unitsButton.addEventListener("click", () => {
            const editUnitId = unitsButton.dataset.editUnitId || "";
            delete unitsButton.dataset.editUnitId;
            const existingEditor = card.querySelector("[data-unit-editor]");
            if (existingEditor) {
                if (!editUnitId) {
                    existingEditor.remove();
                    return;
                }
                const existingSelect = existingEditor.querySelector("[data-unit-select]");
                existingSelect.value = editUnitId;
                existingSelect.dispatchEvent(new Event("change"));
                return;
            }
            const editor = document.createElement("div"); editor.dataset.unitEditor = "1";
            editor.style.cssText = "display:flex;flex-direction:column;gap:5px;padding-top:5px;border-top:1px solid #343d4b;font-size:10px;";
            const unitSelect = document.createElement("select");
            unitSelect.dataset.unitSelect = "1";
            unitSelect.style.cssText = `${chromeInputCss()}display:none;`;
            const explanation = document.createElement("div");
            explanation.style.color = COLORS.textDim;
            explanation.textContent = "Subjects are provider-neutral groupings over referenced media and give stable prompt identities to one or more Reference-library members. Subject-class References create one automatically; context-class References do not. Author your own to combine members into a composite Subject. This does not create Subjects for characters mentioned only in shot prose.";
            const create = document.createElement("option"); create.value = ""; create.textContent = "New authored Subject";
            unitSelect.appendChild(create);
            for (const unit of host._promptSemanticUnits || []) {
                const option = document.createElement("option"); option.value = unit.semantic_unit_id;
                option.textContent = `${unit.name || unit.semantic_unit_id}${unit.generated
                    ? " (Reference-generated)" : " (authored)"}`;
                unitSelect.appendChild(option);
            }
            const name = document.createElement("input"); name.placeholder = "Subject name"; name.style.cssText = chromeInputCss();
            const definition = document.createElement("textarea"); definition.placeholder = "Describe this Subject for prompt use…";
            definition.rows = 2; definition.style.cssText = chromeInputCss();
            const memberList = document.createElement("div");
            memberList.style.cssText = "display:flex;flex-wrap:wrap;gap:4px;max-height:110px;overflow:auto;";
            const checks = [];
            for (const reference of host._references || []) {
                for (const member of reference.members || []) {
                    const label = document.createElement("label");
                    label.style.cssText = "display:flex;gap:3px;align-items:center;padding:2px 5px;border:1px solid #3b4656;border-radius:999px;";
                    const box = document.createElement("input"); box.type = "checkbox";
                    checks.push({ box, entity_id: reference.reference_id, member_id: member.member_id });
                    label.append(box, `${reference.name || "Reference"} / ${member.prompt || member.member_id}`);
                    memberList.appendChild(label);
                }
            }
            const visual = document.createElement("select"); visual.style.cssText = chromeInputCss();
            [["preserve", "Fully preserve"], ["partial", "Partially preserve"],
                ["transfer_attributes", "Transfer attributes"], ["reference_loosely", "Weak reference"]]
                .forEach(([value, label]) => { const option = document.createElement("option");
                    option.value = value; option.textContent = label; visual.appendChild(option); });
            const audio = document.createElement("select"); audio.style.cssText = chromeInputCss();
            [["copy_full", "Fully copy audio"], ["copy_partial", "Partially copy audio"],
                ["reference_characteristics", "Reference audio characteristics"], ["reference_loosely", "Weak audio reference"]]
                .forEach(([value, label]) => { const option = document.createElement("option");
                    option.value = value; option.textContent = label; audio.appendChild(option); });
            const membershipReason = document.createElement("div");
            membershipReason.style.cssText = `color:${COLORS.warningText};display:none;`;
            membershipReason.textContent = "Select at least one Reference member before saving this Subject.";
            const generatedNotice = document.createElement("div");
            generatedNotice.style.cssText = `color:${COLORS.textDim};display:none;`;
            generatedNotice.textContent = "This Subject is generated from a Reference. Remove the owning Reference to remove this generated Subject.";
            const save = makeBtn("Save Subject", "One Reference asset may feed several Subjects; one Subject may combine several assets", "primary");
            const remove = makeBtn("Delete authored Subject", "Delete this authored Subject after reviewing its dependents", "danger");
            const updateMemberRule = () => {
                const hasMembers = checks.some((entry) => entry.box.checked);
                save.disabled = !hasMembers;
                membershipReason.style.display = hasMembers ? "none" : "block";
            };
            const loadUnit = () => {
                const unit = (host._promptSemanticUnits || []).find(
                    (value) => value.semantic_unit_id === unitSelect.value);
                name.value = unit?.name || ""; definition.value = unit?.definition || "";
                visual.value = unit?.visual_intent || "preserve";
                audio.value = unit?.audio_intent || "reference_characteristics";
                const selected = new Set((unit?.source_members || []).map(
                    (value) => `${value.entity_id}:${value.member_id}`));
                checks.forEach((entry) => { entry.box.checked = selected.has(`${entry.entity_id}:${entry.member_id}`); });
                generatedNotice.style.display = unit?.generated ? "block" : "none";
                remove.style.display = unit && !unit.generated ? "inline-flex" : "none";
                updateMemberRule();
            };
            checks.forEach((entry) => entry.box.addEventListener("change", updateMemberRule));
            unitSelect.addEventListener("change", loadUnit);
            unitSelect.value = editUnitId;
            loadUnit();
            save.addEventListener("click", async () => {
                const sourceMembers = checks.filter((entry) => entry.box.checked).map(
                    (entry) => ({ entity_id: entry.entity_id, member_id: entry.member_id }));
                if (!name.value.trim() || !sourceMembers.length) return;
                const current = (host._promptSemanticUnits || []).find(
                    (value) => value.semantic_unit_id === unitSelect.value);
                const nextUnit = { ...(current || {}), semantic_unit_id: current?.semantic_unit_id || `unit:${uid()}`,
                    name: name.value.trim(), order: current?.order ?? (host._promptSemanticUnits || []).length,
                    source_members: sourceMembers, visual_intent: visual.value, audio_intent: audio.value,
                    definition: definition.value, intent_overrides: current?.intent_overrides || {} };
                const next = (host._promptSemanticUnits || []).filter(
                    (value) => value.semantic_unit_id !== nextUnit.semantic_unit_id);
                try {
                    await host._savePromptSemanticUnits?.([...next, nextUnit]); render();
                } catch (error) {
                    notifyWarning(error?.message || "Subject unit was rejected.",
                        { source: "prompt-unit-invalid" });
                }
            });
            remove.addEventListener("click", () => {
                void deleteAuthoredSubject(unitSelect.value);
            });
            editor.append(explanation, unitSelect, name, definition, memberList,
                membershipReason, visual, audio, generatedNotice, save, remove);
            card.appendChild(editor);
        });
        card.appendChild(unitsButton);
        subjectList.querySelectorAll("[data-edit-subject]").forEach((button) => {
            button.addEventListener("click", () => {
                unitsButton.dataset.editUnitId = button.dataset.editSubject;
                unitsButton.click();
            });
        });
        subjectList.querySelectorAll("[data-delete-subject]").forEach((button) => {
            if (button.disabled) return;
            button.addEventListener("click", () => {
                void deleteAuthoredSubject(button.dataset.deleteSubject);
            });
        });

        const commit = async (operations, label) => {
            host._pushUndo?.(label);
            try {
                await host._runSceneMutation(operations, {
                    key: `scene:${host.activeSceneId}:prompt-context:${Date.now()}`,
                    label, coalesce: false, refreshScenes: false,
                    // These operations may carry complete setup/recipe arrays.
                    // Replan after a conflict instead of replaying stale arrays.
                    retryOnConflict: false,
                });
                await host._fetchScenes?.({ ignoreMutationGate: true, reason: "prompt_context" });
                render();
                return true;
            } catch (error) {
                host._discardLastUndo?.(label);
                notifyWarning(error?.message || "Prompt Context change was refused.",
                    { source: "prompt-context-refused" });
                return false;
            }
        };
        profile.addEventListener("change", () => commit([{
            type: "update_scene_fields",
            fields: { prompt_context_profile_id: profile.value },
        }], "change prompt context profile"));

        const setups = Array.isArray(scene.minimax_h3_conditioning_setups)
            ? structuredClone(scene.minimax_h3_conditioning_setups) : [];
        const active = setups.find((value) => value.setup_id === scene.active_minimax_h3_setup_id)
            || (setups.length === 1 ? setups[0] : null);
        const uid = () => globalThis.crypto?.randomUUID?.().replaceAll("-", "")
            || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
        const templateId = host._channelTemplate().id;

        if (templateId === "minimax_h3_base") {
            const controls = document.createElement("div");
            controls.style.cssText = "display:flex;gap:6px;align-items:center;flex-wrap:wrap;";
            const task = document.createElement("select"); task.style.cssText = chromeInputCss();
            for (const value of ["T2VA", "I2VA", "FL2VA", "L2VA"]) {
                const option = document.createElement("option"); option.value = value;
                option.textContent = value; task.appendChild(option);
            }
            task.value = active?.mode === "base" ? active.task_mode || "T2VA" : "T2VA";
            const guideSelect = (label, selected) => {
                const select = document.createElement("select"); select.style.cssText = chromeInputCss();
                const none = document.createElement("option"); none.value = "";
                none.textContent = `${label}: none`; select.appendChild(none);
                for (const guide of scene.guide_frames || []) {
                    if (guide.muted) continue;
                    const option = document.createElement("option"); option.value = guide.guide_id || "";
                    option.textContent = `${label}: ${guide.frame_index === -1 ? "last" : `${guide.frame_index}f`}`;
                    select.appendChild(option);
                }
                select.value = selected || ""; return select;
            };
            const first = guideSelect("First frame", active?.mode === "base" ? active.first_guide_id : "");
            const last = guideSelect("Last frame", active?.mode === "base" ? active.last_guide_id : "");
            const save = makeBtn("Use H3 Base setup", "Bind task mode and physical Guide anchors", "primary");
            save.addEventListener("click", () => {
                const setup = { ...(active?.mode === "base" ? active : {}),
                    schema: "minimax_h3_setup_v1", setup_id: active?.mode === "base" ? active.setup_id : uid(),
                    name: "MiniMax H3 Base Setup", mode: "base", task_mode: task.value,
                    first_guide_id: first.value, last_guide_id: last.value };
                commit([{ type: "update_scene_fields", fields: {
                    prompt_context_profile_config: { task_mode: task.value },
                    minimax_h3_conditioning_setups: [...setups.filter((value) => value.setup_id !== setup.setup_id), setup],
                    active_minimax_h3_setup_id: setup.setup_id,
                } }], "configure MiniMax H3 Base");
            });
            controls.append(task, first, last, save); card.appendChild(controls);
        } else if (templateId === "minimax_h3_ref") {
            const recipes = Array.isArray(scene.reference_lane_recipes)
                ? structuredClone(scene.reference_lane_recipes) : [];
            const populationSpecs = [
                { population: "picture", physical: "pictures", label: "Picture",
                    setupKey: "picture_lane_ids" },
                { population: "video", physical: "videos", label: "Video",
                    setupKey: "video_lane_ids" },
                { population: "audio", physical: "standalone_audios", label: "Audio",
                    setupKey: "audio_lane_ids" },
            ];
            const definitions = populationSpecs.map((spec) => ({ ...spec,
                id: (host._referenceRecipePresets || []).find((preset) =>
                    preset?.soft?.physical_population === spec.physical)?.id || "",
            })).filter((definition) => definition.id);
            const linked = (definition) => active?.mode === "reference"
                && recipes.some((value) => value?.recipe_id === definition.id
                    && (active[definition.setupKey] || []).map(String).includes(
                        String(value?.lane_id || "")));
            const linkedLabels = definitions.filter(linked).map((value) => value.label);
            const status = document.createElement("div");
            status.style.cssText = `font-size:10px;color:${COLORS.textDim};`;
            status.textContent = linkedLabels.length
                ? `Linked populations: ${linkedLabels.join(", ")}. Add only the lanes this scene needs.`
                : "No H3 Reference population is linked. Add only the lanes this scene needs.";
            const actions = document.createElement("div");
            actions.style.cssText = "display:flex;gap:6px;align-items:center;flex-wrap:wrap;";
            const populationButtons = [];
            for (const definition of definitions) {
                const isLinked = linked(definition);
                const add = makeBtn(isLinked ? `${definition.label} linked`
                    : `Add ${definition.label} lane`,
                isLinked ? `${definition.label} is already part of the active H3 setup.`
                    : `Create or link only the ${definition.label} Reference population.`,
                isLinked ? "secondary" : "primary");
                add.disabled = isLinked;
                add.addEventListener("click", async () => {
                    // Each plan must read a coherent scene snapshot. Do not let
                    // rapid clicks serialize two stale plans for the same lane.
                    const priorDisabled = populationButtons.map((button) => button.disabled);
                    populationButtons.forEach((button) => { button.disabled = true; });
                    try {
                        const completed = await commit([{
                            type: "ensure_minimax_h3_reference_population",
                            population: definition.population,
                        }], `add MiniMax H3 ${definition.label} Reference lane`);
                        if (completed) return;
                    } catch (error) {
                        notifyWarning(error?.message || "Reference population could not be planned.",
                            { source: "prompt-context-refused" });
                    }
                    populationButtons.forEach((button, index) => {
                        button.disabled = priorDisabled[index];
                    });
                });
                populationButtons.push(add);
                actions.appendChild(add);
            }
            const roleHint = document.createElement("div");
            roleHint.style.cssText = `font-size:10px;color:${COLORS.textDim};`;
            roleHint.textContent = "Roles remain explicit; staging media never implies editing, continuation, or audio reuse.";
            card.append(status, actions, roleHint);
        }
        bodyEl.appendChild(card);
    };

    const render = () => {
        if (!mounted) return;
        for (const editor of body.querySelectorAll("[data-sonder-prompt-box='1']")) {
            editor._sonderPromptContextMenuCleanup?.();
        }
        body.textContent = "";
        const scene = host.activeScene;
        if (!scene) {
            body.appendChild(sectionTitle("No active scene"));
            renderDiagnostics(null);
            return;
        }
        const sectionsLocked = host._isPromptTrackLocked();
        const globalLocked = host._isGlobalPromptTrackLocked();

        renderContextSettings(body, scene);

        // ── Global prompt (auto-commits on Enter/blur; no Save button) ─
        body.appendChild(sectionTitle(`Global Prompt (always-on)${globalLocked ? " — locked" : ""}`));
        // The global prompt is per channel too: under a named-field template a
        // single blob has nowhere correct to go, and MiniMax wants its style
        // opening at the head of the description rather than ahead of the first
        // field name. Which sections take each channel is a per-SECTION choice,
        // shown on the section cards below.
        const globalTemplate = host._channelTemplate();
        // One box per channel, or a single box when the template turns
        // per-channel globals off.
        const globalKeys = globalChannelKeys(globalTemplate);
        const globalChannels = normalizeChannels(
            scene.global_channels, scene.prompt, globalKeys);
        const globalDocuments = { ...(scene.global_channel_docs || {}) };
        let globalAttachments = normalizePromptAttachments(scene.global_attachments);
        const draftGlobalSceneSnapshot = () => sceneWithDraftGlobal(scene, {
            attachments: globalAttachments,
            channelDocs: globalDocuments,
            channels: Object.fromEntries(globalKeys.map((channelKey) => [
                channelKey, promptDocumentText(
                    globalDocuments[channelKey] || normalizePromptDocument(
                        null, globalChannels[channelKey] || "")).trim(),
            ])),
        });
        const globalInputs = {};
        const globalRow = document.createElement("div");
        globalRow.style.cssText = `
            display:grid; gap:6px; align-items:start;
            grid-template-columns: ${globalKeys.map(() => "1fr").join(" ")};
        `;
        for (const key of globalKeys) {
            const channel = (globalTemplate.channels || []).find((c) => c.key === key) || { key };
            const column = document.createElement("div");
            column.style.cssText = "display:flex; flex-direction:column; gap:2px; min-width:0;";
            // Which sections take this text is decided per section now, so the
            // global row is text only.
            const head = document.createElement("div");
            head.style.cssText = `font-size:9px; color:${COLORS.textDim}; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;`;
            head.textContent = key;
            head.title = channel.description
                || `Global ${key}. Each section chooses whether to take it.`;

            const configureGlobalAttachment = (attachment, anchoredChannels = [key]) => configurePromptAttachment(attachment, {
                scene: { ...(scene || {}), _context_channel_keys: globalKeys,
                    _context_reference_frame_threshold: host._referenceFrameThreshold || 0 },
                references: host._references || [], channelKey: key,
                semanticUnits: host._promptSemanticUnits || [],
                profileId: scene.prompt_context_profile_id
                    || globalTemplate.default_context_profile || "generic@1",
                templateId: globalTemplate.id || "",
                scope: "global",
                anchoredChannels,
                taskTypes: host._promptContextCatalog?.minimax_task_types || [],
                managedSpeakerSubjectIds: currentManagedSpeakerSubjectIds(),
                ordinalManifest: currentCandidatePayload()?.ordinal_manifest || {},
                candidate: currentCandidatePayload(),
            });
            const input = createPromptDocumentEditor({
                document: normalizePromptDocument(
                    globalDocuments[key], globalChannels[key] || ""),
                attachments: globalAttachments,
                previews: currentCandidatePayload()?.attachment_previews || {},
                attachmentLabelFor,
                attachmentContext: { scene, template: globalTemplate },
                disabled: globalLocked,
                ariaLabel: channel.description || `Global ${key}`,
                onChange: ({ document: nextDocument, attachments, reason }) => {
                    globalDocuments[key] = nextDocument;
                    globalAttachments = attachments;
                    for (const sibling of Object.values(globalInputs)) {
                        if (sibling === input) continue;
                        if (reason === "attachment") {
                            sibling.recordSharedAttachmentTransaction?.(globalAttachments);
                        } else {
                            sibling.syncPromptAttachments?.(globalAttachments);
                        }
                    }
                    const snapshot = draftGlobalSceneSnapshot();
                    host._previewPromptContextCandidate?.({
                        global_channels: snapshot.global_channels,
                        global_channel_docs: snapshot.global_channel_docs,
                        global_attachments: snapshot.global_attachments,
                    });
                    if (["attachment", "history"].includes(reason)) {
                        queueMicrotask(() => {
                            renderGlobalScope();
                            commitGlobal().catch(() => {});
                        });
                    }
                },
                onActivateAttachment: async (attachment) => {
                    const configured = await configureGlobalAttachment(attachment);
                    if (configured) input.replaceAttachment(configured);
                },
            });
            globalInputs[key] = input;
            input.dataset.sonderPromptGlobalChannel = key;
            applyBoxHeight(host, input, "panelGlobalBoxHeight", 72);
            registerPromptBoxGuard(input, () => globalChannels[key] || "");
            const commitGlobal = async () => {
                if (globalLocked || guard.suppressBlurCommit) return;
                globalChannels[key] = input.value;
                await host._updateSceneGlobalContext(
                    globalChannels, globalDocuments, globalAttachments);
            };
            input.addOwnedKeyHandler?.((e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    commitGlobal().catch(() => {});
                    return true;
                }
                return false;
            });
            input.addEventListener("blur", (event) => {
                if (event.relatedTarget?.closest?.("[data-sonder-prompt-context-modal='1'],[data-sonder-prompt-context-menu='1']")) return;
                commitGlobal().catch(() => {});
            });
            installPromptContextMenu({
                editor: input,
                allowedKinds: ["reference", "custom"],
                onInserted: ({ type }) => type === "writing_aid" ? commitGlobal() : null,
                onCreate: configureGlobalAttachment,
                writingAids: host._promptContextWritingAids?.(
                    scene.prompt_context_profile_id
                        || globalTemplate.default_context_profile || "generic@1") || [],
            });
            let projectionHosts = null;
            const refreshProjection = (payload = currentCandidatePayload()) => {
                const next = createAttachmentChannelProjections({
                    channelKey: key,
                    attachments: globalAttachments,
                    candidate: payload,
                    attachmentLabelFor,
                    onActivate: async (attachment) => {
                        const configured = await configureGlobalAttachment(
                            attachment, promptAttachmentAnchoredChannels(
                                globalDocuments, attachment.attachment_id));
                        if (configured) input.replaceAttachment(configured);
                    },
                    onSetCapabilityEnabled: (attachment, projection, enabled) => {
                        input.replaceAttachment(setPromptAttachmentCapabilityEnabled(
                            attachment, projection, enabled));
                    },
                    onLinkedSuppressionWarning: ({ linkedCount }) => {
                        notifyWarning(
                            `Suppressed here; ${linkedCount} linked chip${linkedCount === 1 ? " can" : "s can"} still emit. Suppress there too, or Unlink first.`,
                            { source: "prompt-linked-suppression" });
                    },
                });
                next.beforeHost._sonderRefreshProjection = refreshProjection;
                projectionHosts?.beforeHost.replaceWith(next.beforeHost);
                projectionHosts?.afterHost.replaceWith(next.afterHost);
                projectionHosts = next;
            };
            refreshProjection();
            column.append(head, createPromptProjectionBox(
                input, projectionHosts.beforeHost, projectionHosts.afterHost));
            if (key === globalKeys[0]) appendPromptContextHint(host, column);
            globalRow.appendChild(column);
        }
        body.appendChild(globalRow);
        const globalScopeHost = document.createElement("div");
        const globalAnchorIds = () => new Set(Object.values(globalDocuments)
            .flatMap((value) => normalizePromptDocument(value).nodes)
            .filter((node) => node.type === "attachment")
            .map((node) => node.attachment_id));
        const configureGlobalScope = (attachment) => configurePromptAttachment(attachment, {
            scene: { ...(scene || {}), _context_channel_keys: globalKeys,
                _context_reference_frame_threshold: host._referenceFrameThreshold || 0 },
            references: host._references || [], channelKey: globalKeys[0] || "visual",
            semanticUnits: host._promptSemanticUnits || [],
            profileId: scene.prompt_context_profile_id
                || globalTemplate.default_context_profile || "generic@1",
            templateId: globalTemplate.id || "",
            scope: "global",
            anchoredChannels: [],
            taskTypes: host._promptContextCatalog?.minimax_task_types || [],
            managedSpeakerSubjectIds: currentManagedSpeakerSubjectIds(),
            ordinalManifest: currentCandidatePayload()?.ordinal_manifest || {},
            candidate: currentCandidatePayload(),
        });
        const commitGlobalScope = async () => {
            const nextChannels = Object.fromEntries(globalKeys.map((key) => [
                key, promptDocumentText(globalDocuments[key]).trim(),
            ]));
            await host._updateSceneGlobalContext(
                nextChannels, globalDocuments, globalAttachments);
        };
        const renderGlobalScope = () => {
            const anchored = globalAnchorIds();
            globalScopeHost.replaceChildren(createScopeChipRow({
                attachments: globalAttachments.filter((value) => !anchored.has(value.attachment_id)),
                previews: currentCandidatePayload()?.attachment_previews || {},
                attachmentLabelFor,
                disabled: globalLocked,
                allowedKinds: ["reference", "custom"],
                onAdd: async (kind) => {
                    const configured = await configureGlobalScope({ kind });
                    if (!configured) return;
                    globalInputs[globalKeys[0]]?.transactPromptAttachments?.(
                        [...globalAttachments, configured]);
                },
                onActivate: async (attachment) => {
                    const configured = await configureGlobalScope(attachment);
                    if (!configured) return;
                    globalInputs[globalKeys[0]]?.transactPromptAttachments?.(
                        globalAttachments.map((value) =>
                            value.attachment_id === configured.attachment_id ? configured : value));
                },
                onRemove: async (attachment) => {
                    globalInputs[globalKeys[0]]?.transactPromptAttachments?.(
                        globalAttachments.filter((value) =>
                            value.attachment_id !== attachment.attachment_id));
                },
            }));
        };
        renderGlobalScope();
        body.appendChild(globalScopeHost);
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
            const template = host._channelTemplate();
            const channelKeys = templateChannelKeys(template);
            // Two rows, not one. Sharing a single grid row with the range
            // inputs and four fixed-width buttons squeezed six channels to
            // ~40px each — one character per line. The channels now own a
            // full-width row of their own beneath the controls.
            const card = document.createElement("div");
            card.style.cssText = `
                display:flex; flex-direction:column; gap:6px; padding:6px 8px;
                background:${COLORS.panel}; border:1px solid ${COLORS.promptBorder}; border-radius:4px;
            `;
            const row = document.createElement("div");
            row.style.cssText = `
                display:grid; grid-template-columns: 58px 58px 1fr auto auto auto auto;
                gap:6px; align-items:center;
            `;
            // The body field takes twice the width of the rest; at one channel
            // it simply fills the row.
            const wideKey = template.shot_marker_channel || channelKeys[0];
            const channelRow = document.createElement("div");
            channelRow.style.cssText = `
                display:grid; gap:6px; align-items:start;
                grid-template-columns: ${channelKeys.map((key) => (key === wideKey ? "2fr" : "1fr")).join(" ")};
            `;
            const startInput = smallInput({ value: String(section.start_frame ?? 0), numeric: true, width: "100%" });
            const endInput = smallInput({ value: String(section.end_frame ?? 0), numeric: true, width: "100%" });
            startInput.title = "Start frame";
            endInput.title = "End frame (exclusive)";
            const channels = normalizeChannels(section.channels, section.prompt, channelKeys);
            const channelInputs = {};
            const channelDocuments = { ...(section.channel_docs || {}) };
            let sectionAttachments = normalizePromptAttachments(section.attachments);
            const draftSceneSnapshot = () => sceneWithDraftSection(scene, {
                index: idx,
                promptId: section.prompt_id || "",
                startFrame: Number(startInput.value),
                endFrame: Number(endInput.value),
                section,
                channels: Object.fromEntries(channelKeys.map((channelKey) => [
                    channelKey, promptDocumentText(
                        channelDocuments[channelKey] || normalizePromptDocument(
                            null, channels[channelKey] || "")).trim(),
                ])),
                channelDocs: channelDocuments,
                attachments: sectionAttachments,
            });
            const allSectionAttachments = () => (draftSceneSnapshot().prompt_sections || [])
                .flatMap((value) => normalizePromptAttachments(value.attachments));
            const isLinkedAttachment = (attachment) => allSectionAttachments()
                .filter((value) => value.emission_group_id
                    === attachment.emission_group_id).length > 1;
            for (const channel of template.channels || []) {
                const key = channel.key;
                const column = document.createElement("div");
                column.style.cssText = "display:flex; flex-direction:column; gap:2px; min-width:0;";
                // A standing label, not just a placeholder: the placeholder
                // disappears once a box has text, which is precisely when a
                // six-channel row most needs to say which field is which.
                const caption = document.createElement("div");
                caption.style.cssText = `font-size:9px; color:${COLORS.textDim}; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;`;
                caption.textContent = key;
                caption.title = channel.description || `${key} channel`;
                const configureChannelAttachment = (attachment, anchoredChannels = [key]) => configurePromptAttachment(attachment, {
                    scene: { ...(scene || {}), _context_channel_keys: channelKeys,
                        _context_consumer_start: section.start_frame,
                        _context_consumer_end: section.end_frame,
                        _context_reference_frame_threshold: host._referenceFrameThreshold || 0 },
                    references: host._references || [], channelKey: key,
                    semanticUnits: host._promptSemanticUnits || [],
                    profileId: scene.prompt_context_profile_id
                        || template.default_context_profile || "generic@1",
                    templateId: template.id || "",
                    scope: "section",
                    anchoredChannels,
                    taskTypes: host._promptContextCatalog?.minimax_task_types || [],
                    managedSpeakerSubjectIds: currentManagedSpeakerSubjectIds(),
                    ordinalManifest: currentCandidatePayload()?.ordinal_manifest || {},
                    candidate: currentCandidatePayload(),
                });
                const input = createPromptDocumentEditor({
                    document: normalizePromptDocument(
                        channelDocuments[key], channels[key] || ""),
                    attachments: sectionAttachments,
                    previews: currentCandidatePayload()?.attachment_previews || {},
                    attachmentLabelFor,
                    attachmentContext: { scene, template },
                    disabled: sectionsLocked,
                    ariaLabel: channel.description || `${key} channel`,
                    onChange: ({ document: nextDocument, attachments, reason }) => {
                        channelDocuments[key] = nextDocument;
                        sectionAttachments = attachments;
                        for (const sibling of Object.values(channelInputs)) {
                            if (sibling === input) continue;
                            if (reason === "attachment") {
                                sibling.recordSharedAttachmentTransaction?.(sectionAttachments);
                            } else {
                                sibling.syncPromptAttachments?.(sectionAttachments);
                            }
                        }
                        const candidateSections = draftSceneSnapshot().prompt_sections;
                        host._previewPromptContextCandidate?.({
                            prompt_sections: candidateSections,
                        });
                        if (["attachment", "history"].includes(reason)) {
                            queueMicrotask(() => {
                                renderScope();
                                commitChannels().catch(() => render());
                            });
                        }
                    },
                    onActivateAttachment: async (attachment) => {
                        const configured = await configureChannelAttachment(attachment);
                        if (!configured) return;
                        if (isLinkedAttachment(attachment)) {
                            await host._updateLinkedPromptAttachment?.(
                                attachment.attachment_id, configured);
                            render();
                            return;
                        }
                        input.replaceAttachment(configured);
                    },
                });
                applyBoxHeight(host, input, "panelChannelBoxHeight", 72);
                registerPromptBoxGuard(input, () => channels[key] || "");
                channelInputs[key] = input;
                installPromptContextMenu({
                    editor: input,
                    onInserted: ({ type }) => type === "writing_aid" ? commitChannels() : null,
                    onCreate: configureChannelAttachment,
                    writingAids: host._promptContextWritingAids?.(
                        scene.prompt_context_profile_id
                            || template.default_context_profile || "generic@1") || [],
                });
                let projectionHosts = null;
                const refreshProjection = (payload = currentCandidatePayload()) => {
                    const next = createAttachmentChannelProjections({
                        channelKey: key,
                        attachments: sectionAttachments,
                        candidate: payload,
                        attachmentLabelFor,
                        onActivate: async (attachment) => {
                            const configured = await configureChannelAttachment(
                                attachment, promptAttachmentAnchoredChannels(
                                    channelDocuments, attachment.attachment_id));
                            if (!configured) return;
                            if (isLinkedAttachment(attachment)) {
                                await host._updateLinkedPromptAttachment?.(
                                    attachment.attachment_id, configured);
                                render();
                                return;
                            }
                            input.replaceAttachment(configured);
                        },
                        onSetCapabilityEnabled: (attachment, projection, enabled) => {
                            input.replaceAttachment(setPromptAttachmentCapabilityEnabled(
                                attachment, projection, enabled));
                        },
                        onLinkedSuppressionWarning: ({ linkedCount }) => {
                            notifyWarning(
                                `Suppressed here; ${linkedCount} linked chip${linkedCount === 1 ? " can" : "s can"} still emit. Suppress there too, or Unlink first.`,
                                { source: "prompt-linked-suppression" });
                        },
                    });
                    next.beforeHost._sonderRefreshProjection = refreshProjection;
                    projectionHosts?.beforeHost.replaceWith(next.beforeHost);
                    projectionHosts?.afterHost.replaceWith(next.afterHost);
                    projectionHosts = next;
                };
                refreshProjection();
                column.append(caption, createPromptProjectionBox(
                    input, projectionHosts.beforeHost, projectionHosts.afterHost));
                channelRow.appendChild(column);
            }

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
                const next = {};
                for (const key of channelKeys) {
                    next[key] = channelInputs[key].value.trim();
                }
                const nextDocuments = Object.fromEntries(
                    channelKeys.map((key) => [key, channelInputs[key].promptDocument]));
                await host._updatePromptSection(idx, {
                    channels: next,
                    channel_docs: nextDocuments,
                    attachments: sectionAttachments,
                });
                // A channel-only save cannot reorder rows. Keep these mounted
                // editors alive so their shared attachment/caret/undo histories
                // remain authoritative across the completed backend write.
            };
            const scopeHost = document.createElement("div");
            const configureScope = (attachment) => configurePromptAttachment(attachment, {
                scene: { ...(scene || {}), _context_channel_keys: channelKeys,
                    _context_consumer_start: section.start_frame,
                    _context_consumer_end: section.end_frame,
                    _context_reference_frame_threshold: host._referenceFrameThreshold || 0 },
                references: host._references || [], channelKey: wideKey,
                semanticUnits: host._promptSemanticUnits || [],
                profileId: scene.prompt_context_profile_id
                    || template.default_context_profile || "generic@1",
                templateId: template.id || "",
                scope: "section",
                anchoredChannels: [],
                taskTypes: host._promptContextCatalog?.minimax_task_types || [],
                managedSpeakerSubjectIds: currentManagedSpeakerSubjectIds(),
                ordinalManifest: currentCandidatePayload()?.ordinal_manifest || {},
                candidate: currentCandidatePayload(),
            });
            const renderScope = () => {
                const anchored = new Set(Object.values(channelDocuments)
                    .flatMap((value) => normalizePromptDocument(value).nodes)
                    .filter((node) => node.type === "attachment")
                    .map((node) => node.attachment_id));
                scopeHost.replaceChildren(createScopeChipRow({
                    attachments: sectionAttachments.filter((value) => !anchored.has(value.attachment_id)),
                    previews: currentCandidatePayload()?.attachment_previews || {},
                    attachmentLabelFor,
                    disabled: sectionsLocked,
                    allowedKinds: template.shot_marker_channel
                        ? ["shot", "timestamp", "reference", "custom"]
                        : ["reference", "custom"],
                    reusableAttachments: allSectionAttachments(),
                    allSceneAttachments: allSectionAttachments(),
                    reuseContext: { scene: draftSceneSnapshot(), template },
                    onReuse: async (configured) => {
                        channelInputs[wideKey]?.transactPromptAttachments?.(
                            [...sectionAttachments, configured]);
                    },
                    onAdd: async (kind) => {
                        const configured = await configureScope({ kind });
                        if (!configured) return;
                        channelInputs[wideKey]?.transactPromptAttachments?.(
                            [...sectionAttachments, configured]);
                    },
                    onActivate: async (attachment) => {
                        const configured = await configureScope(attachment);
                        if (!configured) return;
                        if (isLinkedAttachment(attachment)) {
                            await host._updateLinkedPromptAttachment?.(
                                attachment.attachment_id, configured);
                            render();
                            return;
                        }
                        channelInputs[wideKey]?.transactPromptAttachments?.(
                            sectionAttachments.map((value) =>
                                value.attachment_id === configured.attachment_id ? configured : value));
                    },
                    onUnlink: async (configured) => {
                        channelInputs[wideKey]?.transactPromptAttachments?.(
                            sectionAttachments.map((value) =>
                                value.attachment_id === configured.attachment_id ? configured : value));
                    },
                    onRemove: async (attachment) => {
                        channelInputs[wideKey]?.transactPromptAttachments?.(
                            sectionAttachments.filter((value) =>
                                value.attachment_id !== attachment.attachment_id));
                    },
                }));
            };
            renderScope();
            const onEnterBlur = (input, commit) => {
                const handler = (e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        commit().catch(() => render());
                        return true;
                    }
                    return false;
                };
                if (input.addOwnedKeyHandler) input.addOwnedKeyHandler(handler);
                else input.addEventListener("keydown", (e) => {
                    handler(e);
                    e.stopPropagation();
                });
                input.addEventListener("blur", (event) => {
                if (event.relatedTarget?.closest?.("[data-sonder-prompt-context-modal='1'],[data-sonder-prompt-context-menu='1']")) return;
                    commit().catch(() => render());
                });
            };
            for (const el of [startInput, endInput]) { el.disabled = sectionsLocked; onEnterBlur(el, commitRange); }
            for (const key of channelKeys) {
                const el = channelInputs[key];
                el.disabled = sectionsLocked;
                onEnterBlur(el, commitChannels);
            }

            const selectBtn = makeBtn("Select", "Set selection to this section");
            selectBtn.addEventListener("click", () => {
                host._setSelectionToFrameRange(section.start_frame || 0, section.end_frame || 0);
            });
            const queueBtn = makeBtn("Queue", "Queue this prompt section (Batch toggle picks chunked vs single job)");
            queueBtn.addEventListener("click", () => { host._queuePromptSection(section).catch(() => {}); });
            const queueAdvisory = document.createElement("span");
            queueAdvisory.dataset.sonderQueueAdvisoryCount = "1";
            queueAdvisory.style.cssText = `display:none;font-size:9px;color:${COLORS.warningText};white-space:nowrap;`;
            const addAfterBtn = makeBtn("+", "Insert a new section after this one (fills the gap to the next section)");
            addAfterBtn.disabled = sectionsLocked;
            addAfterBtn.addEventListener("click", async () => {
                if (sectionsLocked) return;
                const created = await host._addPromptSectionAfter(idx).catch(() => false);
                if (created) render();
            });
            const deleteBtn = makeBtn("✕", "Delete this section", "danger");
            deleteBtn.style.cssText += "min-width:22px;min-height:22px;padding:1px 5px;font-size:10px;font-weight:500;";
            deleteBtn.disabled = sectionsLocked;
            deleteBtn.addEventListener("click", async () => {
                if (sectionsLocked) return;
                if (!confirm("Delete this prompt section?")) return;
                await host._deletePromptSection(idx);
                render();
            });

            // The 1fr spacer keeps the buttons right-aligned now that the
            // channels no longer occupy the middle of the controls row.
            const spacer = document.createElement("div");
            row.append(startInput, endInput, spacer,
                       selectBtn, queueBtn, queueAdvisory, addAfterBtn, deleteBtn);
            card.append(row, channelRow, scopeHost);

            // Which scene-global channels this section takes. One checkbox per
            // global channel, so a template with global channels off shows
            // exactly one. Only channels that actually carry text are offered —
            // a checkbox for an empty global does nothing either way.
            const inheritKeys = globalKeys.filter((key) => (globalChannels[key] || "").trim());
            if (inheritKeys.length) {
                const inheritRow = document.createElement("div");
                inheritRow.style.cssText = `display:flex; gap:10px; align-items:center; flex-wrap:wrap; font-size:9px; color:${COLORS.textDim};`;
                const lead = document.createElement("span");
                lead.textContent = inheritKeys.length > 1 ? "Takes global:" : "Takes global";
                lead.title = "Scene-global text this section takes. Unticked, the"
                    + " global text is dropped from any render that only covers"
                    + " sections which opted out.";
                inheritRow.appendChild(lead);
                for (const key of inheritKeys) {
                    const wrap = document.createElement("label");
                    wrap.style.cssText = `display:flex; align-items:center; gap:3px;${sectionsLocked ? " opacity:0.45;" : ""}`;
                    wrap.title = `Take the scene-global ${key} in this section.`;
                    const box = document.createElement("input");
                    box.type = "checkbox";
                    box.checked = sectionInheritsGlobal(section, key);
                    box.disabled = sectionsLocked;
                    box.addEventListener("keydown", (e) => e.stopPropagation());
                    box.addEventListener("change", () => {
                        host._setSectionGlobalInherit(idx, key, box.checked)
                            .then(() => render())
                            .catch(() => render());
                    });
                    wrap.append(box);
                    if (inheritKeys.length > 1) {
                        const text = document.createElement("span");
                        text.textContent = key;
                        wrap.append(text);
                    }
                    inheritRow.appendChild(wrap);
                }
                card.appendChild(inheritRow);
            }
            list.appendChild(card);
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
                        const composed = composeSectionText(
                            normalizeChannels(s.channels, s.prompt,
                                              templateChannelKeys(host._channelTemplate())),
                            false, host._channelTemplate());
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
        renderDiagnostics();
    };

    render();
    document.body.appendChild(backdrop);

    const handle = {
        element: backdrop,
        refresh: render,
        refreshDiagnostics: renderDiagnostics,
        cleanup: close,
        isMounted: () => mounted,
    };
    return handle;
}
