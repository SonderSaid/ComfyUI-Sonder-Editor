// Inline styles for the Controls panel (SDK v2 Phase 4), from editor_theme.js
// tokens only. Rows put each setting on one line; Cards give each one a card.
// The group header matches the Asset Gallery's folder header (SR-010): a small
// bordered toggle, a pointer, the name and a count.
import { EDITOR_COLORS as COLORS, FONT, RADIUS, SPACE, TYPE } from "./editor_theme.js";

// The gallery's disclosure toggle is an 18px square (shared_asset_gallery.js).
const TOGGLE_SIZE = SPACE.s4 + 2;

export const bodyCss = cards => (cards
    ? `display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:${SPACE.s1 + 2}px;align-items:start;`
    : `display:flex;flex-direction:column;gap:0;`);

export const rowCss = ({ cards, wide }) => (cards
    ? `display:flex;flex-direction:column;gap:${SPACE.s1}px;min-width:0;position:relative;box-sizing:border-box;`
        + `padding:${SPACE.s1 + 2}px ${SPACE.s2}px;background:${COLORS.panelRaised};border:1px solid ${COLORS.border};`
        + `border-radius:${RADIUS.r3}px;${wide ? "grid-column:1 / -1;" : ""}`
    : `display:flex;flex-direction:column;gap:2px;min-width:0;position:relative;box-sizing:border-box;`
        + `padding:${SPACE.s1}px 2px;border-bottom:1px solid ${COLORS.borderSoft};`);

// The label/control line. Rows: one line, label left, control right (the
// label column ellipsizes and keeps the full name in its title). Cards: the
// label above the control, except an on/off setting, which stays on one line.
export const lineCss = ({ cards, stacked, inline }) => {
    if (cards && !inline) return `display:flex;flex-direction:column;gap:${SPACE.s1}px;min-width:0;`;
    if (stacked) return `display:flex;flex-direction:column;gap:${SPACE.s1}px;min-width:0;`;
    return `display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.25fr);gap:${SPACE.s1}px ${SPACE.s3}px;align-items:center;min-width:0;`
        + (cards ? "grid-template-columns:minmax(0,1fr) auto;" : "");
};

export const labelCss = cards => `display:flex;align-items:center;gap:${SPACE.s1 + 1}px;min-width:0;`
    + `color:${cards ? COLORS.textDim : COLORS.textSecondary};font-size:${cards ? TYPE.t11 : TYPE.t12}px;`;

export const labelTextCss = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;";

export const controlCss = ({ cards, alignEnd }) => `display:flex;gap:${SPACE.s1}px;align-items:center;min-width:0;`
    + (alignEnd && !cards ? "justify-content:flex-end;" : "");

export const inputCss = `width:100%;min-width:0;box-sizing:border-box;border:1px solid ${COLORS.border};`
    + `border-radius:${RADIUS.r2}px;background:${COLORS.panelMuted};color:${COLORS.text};`
    + `font:${TYPE.t11}px ${FONT.sans};padding:3px ${SPACE.s1 + 2}px;font-variant-numeric:tabular-nums;`
    + "box-shadow:inset 0 1px 0 rgba(255,255,255,0.03);";

export const numberInputCss = cards => inputCss + (cards ? "" : "width:76px;flex:0 0 auto;text-align:right;");
export const seedInputCss = inputCss + "flex:1 1 auto;";
export const modeSelectCss = inputCss + "width:96px;flex:0 0 auto;cursor:pointer;";

export const noteCss = `font-size:${TYPE.t11}px;line-height:1.45;color:${COLORS.textDim};overflow-wrap:anywhere;margin:0;`;

// A standalone note: an instruction for the whole workflow or its group.
export const standaloneNoteCss = `font-size:${TYPE.t11}px;line-height:1.5;color:${COLORS.textSecondary};overflow-wrap:anywhere;`
    + `padding:${SPACE.s1 + 2}px ${SPACE.s2}px;background:${COLORS.accentSoft};border-left:2px solid ${COLORS.accentLo};`
    + `border-radius:0 ${RADIUS.r2}px ${RADIUS.r2}px 0;`;

export const tagCss = `font-size:${TYPE.t10 - 1}px;font-weight:${TYPE.fwBold};letter-spacing:0.04em;text-transform:uppercase;`
    + `color:${COLORS.textDim};border:1px solid ${COLORS.border};border-radius:${RADIUS.r1}px;padding:0 ${SPACE.s1}px;`
    + "line-height:1.5;flex:0 0 auto;";

export const changedDotCss = `width:6px;height:6px;border-radius:50%;background:${COLORS.warningBorder};flex:0 0 auto;`;

export const groupCss = `display:flex;flex-direction:column;grid-column:1 / -1;margin-top:${SPACE.s1 + 2}px;min-width:0;`;

export const groupHeaderCss = `display:flex;align-items:center;justify-content:space-between;gap:${SPACE.s2}px;`
    + `padding:5px ${SPACE.s2}px;border-radius:${RADIUS.r3}px;background:${COLORS.panelRaised};color:${COLORS.textSecondary};`
    + `font-size:${TYPE.t10}px;font-weight:${TYPE.fwBold};cursor:pointer;user-select:none;`;

export const groupToggleCss = `width:${TOGGLE_SIZE}px;height:${TOGGLE_SIZE}px;border-radius:${RADIUS.r2}px;`
    + `border:1px solid ${COLORS.border};background:${COLORS.panelRaised};color:${COLORS.textSecondary};cursor:pointer;`
    + `font-size:${TYPE.t10}px;line-height:1;padding:0;flex:0 0 auto;font-family:${FONT.sans};`;

export const groupContentCss = cards => `padding-top:${cards ? SPACE.s1 + 2 : 2}px;min-width:0;` + bodyCss(cards);

export const countCss = `color:${COLORS.textDim};font-size:${TYPE.t10}px;font-weight:${TYPE.fwMed};font-variant-numeric:tabular-nums;`;

export const searchCss = inputCss + `flex:1 1 auto;padding:${SPACE.s1}px ${SPACE.s2}px;`;

// The discreet hidden-Advanced line: its only in-panel switch.
export const quietLineCss = `align-self:flex-start;background:none;border:0;padding:0;cursor:pointer;`
    + `font:${TYPE.t10}px ${FONT.sans};color:${COLORS.textMuted};text-align:left;`;

export const linkCss = `background:none;border:0;padding:0;cursor:pointer;font:${TYPE.t10}px ${FONT.sans};color:${COLORS.textDim};`;
