// Bounded declaration editor for a custom Prompt Format.
//
// Module-host contract: the host (editor_prompt_panel) owns the profile catalog,
// networking, and the durable create/migrate writes; this module owns its DOM
// and listeners, returns a cleanup handle, and projects the authored controls
// back into a definition through `collect()`.
//
// The bounded line this module enforces: everything here is DECLARATIVE —
// routing, placement, enum vocabularies, label templates, labels, descriptions,
// examples, help, identity kinds, populations, contributions, and speaker
// policy. Sentence assembly for definitions, retention, voice, and vocal events
// stays specialized server Python, and nothing authored here is ever executed.
// Revisit when a second non-MiniMax format needs different definition sentence
// shapes; until then specialized code is correct and cheaper than a grammar.
//
// It also never invents provider vocabulary. Capability kinds, placement
// phases, and channel keys all arrive from the server catalog and the bound
// channel template; the browser only selects among what was served.

import { EDITOR_COLORS as COLORS, chromeButtonCss, chromeInputCss, setButtonVariant } from "./editor_theme.js";
import { createDisclosureMemory } from "./disclosure_memory.js";

const DISCLOSURE_KEY = "sonder.promptFormatEditor.disclosure.v1";

/** One `value = Label` per line. The single vocabulary spelling in this
 *  module: roles, contributions, and capability field enums all use it, so a
 *  label can never be lost by one surface spelling it differently.
 *
 *  Split on newlines ONLY. A comma is legal inside a label ("Wide, establishing"),
 *  and splitting on it would mint a second bogus value out of the label's tail.
 *
 *  `previous` carries forward per-value metadata this two-column text form
 *  cannot express — today that is `description`. Without it, re-entering a
 *  textarea would silently drop an authored description, which is the same
 *  class of loss as flattening `{value,label}` to `value`.
 */
export function parseDeclaredVocabulary(text, previous = []) {
    const carried = new Map();
    for (const entry of Array.isArray(previous) ? previous : []) {
        if (entry && typeof entry === "object" && entry.value && entry.description) {
            carried.set(String(entry.value), String(entry.description));
        }
    }
    const values = [];
    const seen = new Set();
    for (const line of String(text ?? "").split("\n")) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        const split = trimmed.indexOf("=");
        const value = (split >= 0 ? trimmed.slice(0, split) : trimmed).trim();
        const label = (split >= 0 ? trimmed.slice(split + 1) : trimmed).trim();
        if (!value || seen.has(value)) continue;
        seen.add(value);
        values.push({
            value,
            label: label || value,
            ...(carried.has(value) ? { description: carried.get(value) } : {}),
        });
    }
    return values;
}

export function formatDeclaredVocabulary(values) {
    return (Array.isArray(values) ? values : []).map((entry) => {
        const value = typeof entry === "string" ? entry : String(entry?.value || "");
        const label = typeof entry === "string" ? entry : String(entry?.label || value);
        if (!value) return "";
        return label && label !== value ? `${value} = ${label}` : value;
    }).filter(Boolean).join("\n");
}

/** Bounded lowercase identifier list (media kinds, and nothing else so far). */
export function parseIdentifierList(text) {
    const values = [];
    for (const raw of String(text ?? "").split(/[\s,]+/)) {
        const value = raw.trim().toLowerCase();
        if (value && !values.includes(value)) values.push(value);
    }
    return values;
}

/** Populations whose vocabularies the editor offers a row for. `role_catalogs`
 *  keys are included even when no matching population is declared, so an
 *  authored catalog is never invisible — and therefore never silently lost. */
export function vocabularyPopulations(definition) {
    const keys = [];
    for (const entry of definition?.physical_populations || []) {
        const key = String(entry?.key || "");
        if (key && !keys.includes(key)) keys.push(key);
    }
    for (const key of Object.keys(definition?.role_catalogs || {})) {
        if (key && !keys.includes(key)) keys.push(key);
    }
    return keys;
}

/** Project the authored control state back onto the source definition.
 *
 *  Every declaration group is optional: an untouched group must pass through
 *  byte-for-byte from the fork seed rather than being rebuilt from whatever the
 *  browser happened to render. That is what keeps forking non-destructive.
 */
export function assemblePromptFormatDefinition(base, edits = {}) {
    const definition = structuredClone(base || {});
    // `capabilities.custom`, `validators`, and `writing_aids` are deliberately
    // NOT handled here: the panel owns those three controls and applies them
    // after this call. A second construction of them in this module would be a
    // duplicated authority with no caller, which is how the two halves drift.
    const capabilities = structuredClone(definition.capabilities || {});
    if (edits.derived) {
        const declared = Object.hasOwn(capabilities, "reference");
        const reference = structuredClone(capabilities.reference || {});
        // A migration replaces `routes` wholesale; an editor save must never
        // resurrect it beside the derived declaration it was converted into.
        delete reference.routes;
        reference.derived = structuredClone(edits.derived);
        capabilities.reference = reference;
        // Removing the last part on a format that never declared `reference`
        // returns it to "no Reference contribution". A format that DID declare
        // one keeps the now-empty declaration visible so validation reports it,
        // rather than the editor quietly repairing authored intent.
        if (!declared && !Object.keys(reference.derived).length) {
            delete capabilities.reference;
        }
    }
    definition.capabilities = capabilities;
    if (edits.role_catalogs) {
        const catalogs = {};
        for (const [population, values] of Object.entries(edits.role_catalogs)) {
            if (values?.length) catalogs[population] = structuredClone(values);
        }
        definition.role_catalogs = catalogs;
    }
    if (edits.physical_populations) {
        definition.physical_populations = structuredClone(edits.physical_populations);
    }
    if (edits.identity_kinds) {
        definition.identity_kinds = structuredClone(edits.identity_kinds);
    }
    if (edits.contribution_catalog !== undefined) {
        // For CONTRIBUTIONS the two are genuinely different declarations: an
        // absent key inherits the shared default, an explicit `{}` is a
        // deliberate opt-out, and the server preserves that distinction.
        if (edits.contribution_catalog === null) delete definition.contribution_catalog;
        else definition.contribution_catalog = structuredClone(edits.contribution_catalog);
    }
    if (edits.speaker_policy !== undefined) {
        // Speaker policy has no such distinction to express: the server always
        // materializes the full object and refuses a partial one, so omitting
        // the key stores the default rather than an opt-out. Turn speakers off
        // with `enabled: false`, not by omitting the declaration.
        if (edits.speaker_policy === null) delete definition.speaker_policy;
        else definition.speaker_policy = structuredClone(edits.speaker_policy);
    }
    return definition;
}

function labelled(text, control, { hint = "" } = {}) {
    const row = document.createElement("label");
    row.style.cssText = `display:grid;grid-template-columns:130px minmax(0,1fr);gap:6px;align-items:center;font-size:10px;color:${COLORS.textDim};`;
    const title = document.createElement("span");
    title.textContent = text;
    if (hint) title.title = hint;
    row.append(title, control);
    return row;
}

function input(value = "", { placeholder = "", numeric = false } = {}) {
    const element = document.createElement("input");
    element.type = numeric ? "number" : "text";
    element.value = value === null || value === undefined ? "" : String(value);
    element.placeholder = placeholder;
    element.style.cssText = chromeInputCss({ padding: "4px 6px" });
    return element;
}

function textarea(value = "", { rows = 3, placeholder = "" } = {}) {
    const element = document.createElement("textarea");
    element.value = String(value ?? "");
    element.rows = rows;
    element.placeholder = placeholder;
    element.style.cssText = `${chromeInputCss({ padding: "4px 6px" })};resize:vertical;font:10px/1.4 ui-monospace,monospace;`;
    return element;
}

function select(choices, value, { ariaLabel = "" } = {}) {
    const element = document.createElement("select");
    element.style.cssText = chromeInputCss({ padding: "4px 6px" });
    if (ariaLabel) element.setAttribute("aria-label", ariaLabel);
    for (const choice of choices || []) {
        const option = document.createElement("option");
        option.value = String(choice?.value ?? choice ?? "");
        option.textContent = String(choice?.label ?? choice?.value ?? choice ?? "");
        if (choice?.description) option.title = String(choice.description);
        element.appendChild(option);
    }
    const wanted = String(value ?? "");
    if (wanted && !Array.from(element.options).some((option) => option.value === wanted)) {
        // A stored value outside the served vocabulary stays visible and
        // selected instead of silently snapping to the first option.
        const option = document.createElement("option");
        option.value = wanted;
        option.textContent = `${wanted} — not offered by this build`;
        element.appendChild(option);
    }
    element.value = wanted;
    return element;
}

function button(label, title, variant = "secondary", ariaLabel = "") {
    const element = document.createElement("button");
    element.textContent = label;
    element.title = title || label;
    element.style.cssText = chromeButtonCss();
    setButtonVariant(element, variant);
    element.setAttribute("aria-label", ariaLabel || title || label);
    return element;
}

function note(text) {
    const element = document.createElement("p");
    element.textContent = text;
    element.style.cssText = `margin:0;font-size:9px;line-height:1.5;color:${COLORS.textDim};`;
    return element;
}

/**
 * Mount the declaration groups.
 *
 * `definition` is the fork seed being edited (never mutated). `template` is the
 * bound channel template, `catalog` the server prompt-context catalog. Returns
 * `{ element, collect, cleanup }`; `collect()` projects the authored controls
 * onto the definition and is the only way state leaves this module.
 */
export function mountPromptFormatDeclarationEditor({ definition, template,
    catalog, disclosure = null } = {}) {
    const source = structuredClone(definition || {});
    const memory = disclosure || createDisclosureMemory(DISCLOSURE_KEY);
    const element = document.createElement("div");
    element.dataset.promptFormatDeclarations = "1";
    element.style.cssText = "grid-column:1/-1;display:flex;flex-direction:column;gap:6px;";

    const channelChoices = (template?.channels || []).map((channel) => ({
        value: String(channel?.key || ""),
        label: String(channel?.label || channel?.key || ""),
    })).filter((choice) => choice.value);
    const placementChoices = catalog?.placement_phases || [];
    const capabilityKindChoices = catalog?.reference_capability_kinds || [];

    const groups = [];
    const group = (key, title, { open = false, description = "" } = {}) => {
        const details = document.createElement("details");
        details.dataset.promptFormatGroup = key;
        details.open = memory.isOpen(key, open);
        details.style.cssText = `border:1px solid ${COLORS.promptBorder};border-radius:5px;padding:6px 8px;background:${COLORS.panelRaised};`;
        const summary = document.createElement("summary");
        summary.textContent = title;
        summary.style.cssText = `cursor:pointer;font-size:11px;font-weight:600;color:${COLORS.text};`;
        const body = document.createElement("div");
        body.style.cssText = "display:flex;flex-direction:column;gap:6px;padding-top:6px;";
        if (description) body.appendChild(note(description));
        details.append(summary, body);
        details.addEventListener("toggle", () => memory.remember(key, details.open));
        element.appendChild(details);
        groups.push(details);
        return body;
    };

    // ---- Derived capabilities -------------------------------------------
    const derived = structuredClone(
        source.capabilities?.reference?.derived
        // A legacy format still stores `routes`; it is shown as empty here and
        // repaired by the panel's Migrate action, never converted on save.
        || {});
    const derivedBody = group("derived", "Reference prompt parts", {
        open: true,
        description: "What a Reference contributes, where it lands, and the words this format uses for it. Descriptions, examples, and help are shown beside the controls — they never reach the prompt.",
    });
    const derivedRows = document.createElement("div");
    derivedRows.style.cssText = "display:flex;flex-direction:column;gap:6px;";
    let renderDerived = () => {};

    const fieldEditor = (declaration, kind) => {
        const wrap = document.createElement("div");
        wrap.dataset.promptFormatCapabilityFields = kind;
        wrap.style.cssText = `display:flex;flex-direction:column;gap:5px;padding:5px;border:1px dashed ${COLORS.promptBorder};border-radius:4px;`;
        const heading = document.createElement("strong");
        heading.style.cssText = `font-size:10px;color:${COLORS.textDim};`;
        const fields = declaration.fields && typeof declaration.fields === "object"
            ? declaration.fields : {};
        heading.textContent = `Bounded choices (${Object.keys(fields).length})`;
        wrap.appendChild(heading);
        for (const [name, field] of Object.entries(fields)) {
            const row = document.createElement("div");
            row.style.cssText = "display:flex;flex-direction:column;gap:4px;";
            const header = document.createElement("div");
            header.style.cssText = "display:flex;gap:5px;align-items:center;";
            const nameLabel = document.createElement("code");
            nameLabel.textContent = name;
            nameLabel.style.cssText = `font-size:10px;color:${COLORS.text};`;
            const kindSelect = select(
                [{ value: "enum", label: "Choose one" },
                    { value: "enum_multi", label: "Choose several" }],
                field?.type || "enum", { ariaLabel: `${name} choice mode` });
            kindSelect.addEventListener("change", () => {
                fields[name].type = kindSelect.value;
            });
            const removeField = button("Remove", `Remove the ${name} choices`, "danger",
                `Remove the ${name} bounded choices`);
            removeField.addEventListener("click", () => {
                delete fields[name];
                renderDerived();
            });
            header.append(nameLabel, kindSelect, removeField);
            const labelInput = input(field?.label || "", { placeholder: "Control label" });
            labelInput.addEventListener("input", () => { fields[name].label = labelInput.value; });
            const helpInput = input(field?.help || "", { placeholder: "Help shown beside the control" });
            helpInput.addEventListener("input", () => { fields[name].help = helpInput.value; });
            const values = textarea(formatDeclaredVocabulary(field?.values), {
                rows: 3, placeholder: "value = Label" });
            values.setAttribute("aria-label", `${name} bounded values`);
            values.addEventListener("input", () => {
                // Parse against the CURRENT values so a per-value description
                // survives every edit, not only the first one.
                fields[name].values = parseDeclaredVocabulary(
                    values.value, fields[name].values);
            });
            row.append(header, labelled("Label", labelInput),
                labelled("Help", helpInput),
                labelled("Values", values, { hint: "One `value = Label` per line." }));
            wrap.appendChild(row);
        }
        const adder = document.createElement("div");
        adder.style.cssText = "display:flex;gap:5px;align-items:center;";
        const newName = input("", { placeholder: "field_name" });
        newName.setAttribute("aria-label", `New ${kind} choice field name`);
        const addField = button("Add choices", "Declare a bounded choice field", "secondary",
            `Add bounded choices to ${kind}`);
        addField.addEventListener("click", () => {
            const name = newName.value.trim().toLowerCase().replace(/[^a-z0-9_]+/g, "_");
            if (!name || fields[name]) return;
            declaration.fields = fields;
            fields[name] = { type: "enum", values: [], label: "", help: "" };
            renderDerived();
        });
        adder.append(newName, addField);
        wrap.appendChild(adder);
        return wrap;
    };

    renderDerived = () => {
        derivedRows.replaceChildren();
        const ordered = Object.entries(derived).sort(
            ([leftKind, left], [rightKind, right]) =>
                Number(left?.order || 0) - Number(right?.order || 0)
                || leftKind.localeCompare(rightKind));
        for (const [kind, declaration] of ordered) {
            const row = document.createElement("div");
            row.dataset.promptFormatCapability = kind;
            row.style.cssText = `display:flex;flex-direction:column;gap:5px;padding:6px;border:1px solid ${COLORS.promptBorder};border-radius:4px;`;
            const header = document.createElement("div");
            header.style.cssText = "display:flex;gap:6px;align-items:center;justify-content:space-between;";
            const title = document.createElement("strong");
            title.textContent = String(declaration?.label || kind);
            title.style.cssText = `font-size:11px;color:${COLORS.text};`;
            const kindTag = document.createElement("code");
            kindTag.textContent = kind;
            kindTag.style.cssText = `font-size:9px;color:${COLORS.textDim};`;
            const remove = button("Remove", `Stop declaring ${kind}`, "danger",
                `Stop declaring the ${kind} prompt part`);
            remove.addEventListener("click", () => {
                delete derived[kind];
                renderDerived();
            });
            header.append(title, kindTag, remove);
            const labelInput = input(declaration?.label || "", { placeholder: "Name shown to authors" });
            labelInput.addEventListener("input", () => {
                declaration.label = labelInput.value;
                title.textContent = labelInput.value || kind;
            });
            const channel = select(channelChoices, declaration?.channel_key || "",
                { ariaLabel: `${kind} destination channel` });
            channel.addEventListener("change", () => { declaration.channel_key = channel.value; });
            const placement = select(placementChoices, declaration?.placement || "",
                { ariaLabel: `${kind} placement` });
            placement.addEventListener("change", () => { declaration.placement = placement.value; });
            const order = input(declaration?.order ?? 0, { numeric: true });
            order.setAttribute("aria-label", `${kind} order`);
            order.addEventListener("input", () => {
                declaration.order = Number(order.value) || 0;
            });
            const description = input(declaration?.description || "",
                { placeholder: "What this part is" });
            description.addEventListener("input", () => { declaration.description = description.value; });
            const example = input(declaration?.example || "",
                { placeholder: "Illustrative output" });
            example.addEventListener("input", () => { declaration.example = example.value; });
            const help = input(declaration?.help || "", { placeholder: "Guidance beside the control" });
            help.addEventListener("input", () => { declaration.help = help.value; });
            row.append(header, labelled("Label", labelInput),
                labelled("Channel", channel), labelled("Placement", placement),
                labelled("Order", order), labelled("Description", description),
                labelled("Example", example, {
                    hint: "Illustrative only — an example is never inserted and never compiled." }),
                labelled("Help", help), fieldEditor(declaration, kind));
            derivedRows.appendChild(row);
        }
        if (!ordered.length) {
            derivedRows.appendChild(note(
                "This format declares no Reference prompt parts, so a Reference chip contributes nothing."));
        }
    };
    renderDerived();
    derivedBody.appendChild(derivedRows);
    const addCapabilityRow = document.createElement("div");
    addCapabilityRow.style.cssText = "display:flex;gap:5px;align-items:center;";
    const capabilityKind = select(capabilityKindChoices,
        capabilityKindChoices[0]?.value || "", { ariaLabel: "New prompt part" });
    const addCapability = button("Declare part", "Declare this Reference prompt part",
        "secondary", "Declare a Reference prompt part");
    addCapability.addEventListener("click", () => {
        const kind = capabilityKind.value;
        if (!kind || derived[kind]) return;
        const fallbackLabel = capabilityKindChoices.find(
            (choice) => String(choice.value) === kind)?.label || kind;
        const order = Math.max(0, ...Object.values(derived).map(
            (entry) => Number(entry?.order) || 0)) + 1;
        derived[kind] = {
            order,
            channel_key: channelChoices[0]?.value || "",
            placement: String(placementChoices[0]?.value || "section_prefix"),
            label: fallbackLabel,
            fields: {},
        };
        renderDerived();
    });
    addCapabilityRow.append(capabilityKind, addCapability);
    derivedBody.appendChild(addCapabilityRow);

    // ---- Physical populations -------------------------------------------
    // Declared before the populations block and assigned after the vocabulary
    // block, because population edits drive which vocabulary rows exist. A
    // `const` here would be in its temporal dead zone during the first render.
    let syncVocabularies = () => {};
    const populations = structuredClone(source.physical_populations || []);
    const populationBody = group("populations", "Physical populations", {
        description: "The physical media families this format counts and numbers independently.",
    });
    const populationRows = document.createElement("div");
    populationRows.style.cssText = "display:flex;flex-direction:column;gap:6px;";
    const renderPopulations = () => {
        populationRows.replaceChildren();
        populations.forEach((entry, index) => {
            const row = document.createElement("div");
            row.dataset.promptFormatPopulation = String(entry?.key || index);
            row.style.cssText = `display:flex;flex-direction:column;gap:4px;padding:6px;border:1px solid ${COLORS.promptBorder};border-radius:4px;`;
            const bind = (name, control, read = (value) => value) => {
                control.addEventListener("input", () => { entry[name] = read(control.value); });
                return control;
            };
            const remove = button("Remove", `Remove the ${entry?.key || "population"} population`,
                "danger", `Remove the ${entry?.key || "population"} physical population`);
            remove.addEventListener("click", () => {
                populations.splice(index, 1);
                renderPopulations();
            });
            // A population key rename or removal changes which role and
            // contribution rows exist; without this the new key gets no
            // vocabulary row until the editor is reopened. Bound to `change`
            // (commit) rather than `input` (keystroke) so typing `pic` does not
            // leave behind rows for the intermediate `p` and `pi` keys.
            row.addEventListener("change", () => syncVocabularies());
            row.append(
                labelled("Key", bind("key", input(entry?.key || ""))),
                labelled("Label", bind("label", input(entry?.label || ""))),
                labelled("Ordinal key", bind("ordinal_key", input(entry?.ordinal_key || ""))),
                labelled("Source key", bind("source_key", input(entry?.source_key || ""))),
                labelled("Token kind", bind("token_kind", input(entry?.token_kind || ""))),
                labelled("Label template", bind("label_template",
                    input(entry?.label_template || "", { placeholder: "Picture {n}" })),
                { hint: "May contain only {n}." }),
                labelled("Media kinds", bind("media_kinds",
                    input((entry?.media_kinds || []).join(", "), { placeholder: "image, video" }),
                    parseIdentifierList)),
                labelled("Cap", bind("cap", input(entry?.cap ?? 1, { numeric: true }),
                    (value) => Number(value) || 1)),
                labelled("Description", bind("description", input(entry?.description || ""))),
                remove);
            populationRows.appendChild(row);
        });
    };
    renderPopulations();
    populationBody.appendChild(populationRows);
    const addPopulation = button("Add population", "Declare a physical population",
        "secondary", "Add a physical population");
    addPopulation.addEventListener("click", () => {
        populations.push({ key: "", label: "", ordinal_key: "", source_key: "",
            token_kind: "", label_template: "", media_kinds: ["image"], cap: 1 });
        renderPopulations();
        syncVocabularies();
    });
    populationBody.appendChild(addPopulation);

    // ---- Identity kinds --------------------------------------------------
    const identityKinds = structuredClone(source.identity_kinds || []);
    const identityBody = group("identity_kinds", "Identity kinds", {
        description: "The semantic identities an author may mint, independently of any physical asset.",
    });
    const identityRows = document.createElement("div");
    identityRows.style.cssText = "display:flex;flex-direction:column;gap:6px;";
    const renderIdentityKinds = () => {
        identityRows.replaceChildren();
        identityKinds.forEach((entry, index) => {
            const row = document.createElement("div");
            row.dataset.promptFormatIdentityKind = String(entry?.key || index);
            row.style.cssText = `display:flex;flex-direction:column;gap:4px;padding:6px;border:1px solid ${COLORS.promptBorder};border-radius:4px;`;
            const bind = (name, control, read = (value) => value) => {
                control.addEventListener("input", () => { entry[name] = read(control.value); });
                return control;
            };
            const speaks = document.createElement("input");
            speaks.type = "checkbox";
            speaks.checked = !!entry?.speaks;
            speaks.setAttribute("aria-label", `${entry?.key || "identity"} can speak`);
            speaks.addEventListener("change", () => { entry.speaks = speaks.checked; });
            const remove = button("Remove", `Remove the ${entry?.key || "identity"} kind`,
                "danger", `Remove the ${entry?.key || "identity"} identity kind`);
            remove.addEventListener("click", () => {
                identityKinds.splice(index, 1);
                renderIdentityKinds();
            });
            row.append(
                labelled("Key", bind("key", input(entry?.key || ""))),
                labelled("Label", bind("label", input(entry?.label || ""))),
                labelled("Token kind", bind("token_kind", input(entry?.token_kind || ""))),
                labelled("Referenced label", bind("referenced_label_template",
                    input(entry?.referenced_label_template || "")),
                { hint: "Empty, or containing only {n}." }),
                labelled("Assetless label", bind("assetless_label_template",
                    input(entry?.assetless_label_template || ""))),
                labelled("Speaks", speaks),
                labelled("Description", bind("description", input(entry?.description || ""))),
                remove);
            identityRows.appendChild(row);
        });
    };
    renderIdentityKinds();
    identityBody.appendChild(identityRows);
    const addIdentityKind = button("Add identity kind", "Declare an identity kind",
        "secondary", "Add an identity kind");
    addIdentityKind.addEventListener("click", () => {
        identityKinds.push({ key: "", label: "", token_kind: "",
            referenced_label_template: "", assetless_label_template: "",
            speaks: false });
        renderIdentityKinds();
    });
    identityBody.appendChild(addIdentityKind);

    // ---- Roles and contributions ----------------------------------------
    //
    // Authored text lives in these stores, never in the seed. A re-render reads
    // the store, so editing an unrelated population cannot revert vocabulary the
    // author already typed, and a key whose population was renamed away keeps
    // its text and stays visible for repair instead of being orphaned under a
    // dead key that nothing can reach.
    const roleBody = group("vocabularies", "Roles and contributions", {
        description: "Bounded per-member vocabularies. Each line is `value = Label`; the label is what an author sees and is preserved exactly through a fork.",
    });
    const roleText = new Map(Object.entries(source.role_catalogs || {})
        .map(([population, values]) => [population, formatDeclaredVocabulary(values)]));
    const contributionSource = source.contribution_catalog;
    const contributionText = new Map(Object.entries(contributionSource || {})
        .map(([population, values]) => [population, formatDeclaredVocabulary(values)]));
    const roleHost = document.createElement("div");
    roleHost.style.cssText = "display:flex;flex-direction:column;gap:6px;";
    const contributionHost = document.createElement("div");
    contributionHost.style.cssText = "display:flex;flex-direction:column;gap:6px;";

    /** Keys worth a row: every declared population, plus any key that already
     *  holds authored text. A half-typed population key contributes no text and
     *  so contributes no row. */
    const vocabularyKeys = (store, extra = []) => {
        const keys = [...extra];
        for (const key of vocabularyPopulations({ physical_populations: populations })) {
            if (key && !keys.includes(key)) keys.push(key);
        }
        for (const [key, text] of store) {
            if (key && text.trim() && !keys.includes(key)) keys.push(key);
        }
        return keys;
    };

    const renderVocabularyRows = (host, store, keys, { suffix, placeholder }) => {
        host.replaceChildren();
        const declared = new Set(populations.map((entry) => String(entry?.key || "")));
        for (const population of keys) {
            const control = textarea(store.get(population) || "",
                { rows: 3, placeholder });
            const name = population === "*" ? "All populations" : population;
            control.setAttribute("aria-label", `${population} ${suffix}`);
            control.addEventListener("input", () => store.set(population, control.value));
            const orphaned = population !== "*" && !declared.has(population);
            host.appendChild(labelled(
                orphaned ? `${name} ${suffix} (no such population)` : `${name} ${suffix}`,
                control,
                orphaned ? { hint: "No declared population uses this key, so nothing reads these values. Rename the population back or clear the list." } : {}));
        }
    };

    const renderVocabularies = () => renderVocabularyRows(
        roleHost, roleText, vocabularyKeys(roleText),
        { suffix: "roles", placeholder: "identity = Identity" });
    renderVocabularies();
    roleBody.appendChild(roleHost);

    const contributionToggle = document.createElement("input");
    contributionToggle.type = "checkbox";
    contributionToggle.checked = contributionSource !== undefined
        && contributionSource !== null;
    contributionToggle.setAttribute("aria-label", "Declare this format's own contribution vocabulary");
    roleBody.appendChild(labelled("Own contributions", contributionToggle, {
        hint: "Unchecked inherits the shared default; checked declares this format's own list, and an empty list is a deliberate opt-out.",
    }));
    const renderContributions = () => {
        if (!contributionToggle.checked) {
            contributionHost.replaceChildren();
            return;
        }
        // `"*"` is passed as the leading key, and `vocabularyKeys` dedupes, so a
        // stored `"*"` entry cannot render a second textarea whose edits the
        // later one silently discards.
        renderVocabularyRows(contributionHost, contributionText,
            vocabularyKeys(contributionText, ["*"]),
            { suffix: "contributions", placeholder: "appearance = Appearance" });
    };
    contributionToggle.addEventListener("change", renderContributions);
    renderContributions();
    roleBody.appendChild(contributionHost);
    syncVocabularies = () => { renderVocabularies(); renderContributions(); };

    // ---- Speaker policy --------------------------------------------------
    const policySource = source.speaker_policy;
    const speakerBody = group("speaker_policy", "Speaker policy", {
        description: "How this format spells a speaking identity when several share a line.",
    });
    const policyToggle = document.createElement("input");
    policyToggle.type = "checkbox";
    policyToggle.checked = policySource !== undefined && policySource !== null;
    policyToggle.setAttribute("aria-label", "Declare this format's own speaker policy");
    const policyEnabled = document.createElement("input");
    policyEnabled.type = "checkbox";
    policyEnabled.checked = !!policySource?.enabled;
    policyEnabled.setAttribute("aria-label", "Speakers enabled");
    const policyToken = input(policySource?.token_template ?? "", { placeholder: "(S{n})" });
    policyToken.setAttribute("aria-label", "Speaker token template");
    const policyJoin = input(policySource?.compound_join ?? ",", { placeholder: "," });
    policyJoin.setAttribute("aria-label", "Compound speaker join");
    const policyOrder = select(
        [{ value: "authored", label: "Authored order" },
            { value: "ascending", label: "Ascending ordinal" }],
        policySource?.compound_order || "authored", { ariaLabel: "Compound speaker order" });
    const policyFields = document.createElement("div");
    policyFields.style.cssText = "display:flex;flex-direction:column;gap:5px;";
    policyFields.append(
        labelled("Speakers enabled", policyEnabled),
        labelled("Token template", policyToken, { hint: "May contain only {n}." }),
        labelled("Compound join", policyJoin),
        labelled("Compound order", policyOrder));
    const syncPolicy = () => { policyFields.style.display = policyToggle.checked ? "flex" : "none"; };
    policyToggle.addEventListener("change", syncPolicy);
    syncPolicy();
    speakerBody.append(labelled("Own speaker policy", policyToggle, {
        hint: "Unchecked stores the server default. There is no partial speaker policy — turn speakers off with the checkbox below, not by leaving this one unchecked.",
    }), policyFields);

    const collect = () => {
        const roleCatalogs = {};
        for (const [population, text] of roleText) {
            roleCatalogs[population] = parseDeclaredVocabulary(
                text, source.role_catalogs?.[population]);
        }
        let contributionCatalog = null;
        if (contributionToggle.checked) {
            contributionCatalog = {};
            for (const [population, text] of contributionText) {
                const values = parseDeclaredVocabulary(
                    text, contributionSource?.[population]);
                if (values.length) contributionCatalog[population] = values;
            }
        }
        return assemblePromptFormatDefinition(source, {
            derived,
            role_catalogs: roleCatalogs,
            physical_populations: populations,
            identity_kinds: identityKinds,
            contribution_catalog: contributionCatalog,
            speaker_policy: policyToggle.checked ? {
                enabled: policyEnabled.checked,
                token_template: policyToken.value,
                compound_join: policyJoin.value,
                compound_order: policyOrder.value,
            } : null,
        });
    };

    return {
        element,
        collect,
        cleanup: () => {
            for (const details of groups) details.replaceChildren();
            element.replaceChildren();
        },
    };
}
