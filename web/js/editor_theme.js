// Shared Sonder Editor visual tokens.
// Leaf module: keep this free of editor surface imports so DOM and canvas can share it.

export const THEME = Object.freeze({
    bg0: "#0c1015",
    bg1: "#11161d",
    bg2: "#161c25",
    bg3: "#1d242f",
    bg4: "#262f3c",
    line1: "#1b222c",
    line2: "#2c3744",
    fg0: "#e7ecf2",
    fg1: "#b6bfcb",
    fg2: "#7e8896",
    fgPlaceholder: "#616c7c",
    fg3: "#525c6a",
    accent: "#6686a3",
    accentHi: "#89a4bc",
    accentLo: "#3f566e",
    accentBg: "#1a2530",
    statusIdle: "#7e8896",
    statusRunning: "#6b9e7a",
    statusCompleted: "#6b9e7a",
    statusPending: "#c98a4b",
    statusFailed: "#b26464",
});

export const TYPE = Object.freeze({
    t10: 10,
    t11: 11,
    t12: 12,
    t14: 14,
    t16: 16,
    fwNormal: 400,
    fwMed: 500,
    fwBold: 600,
});

export const SPACE = Object.freeze({
    s1: 4,
    s2: 8,
    s3: 12,
    s4: 16,
    s5: 24,
});

export const RADIUS = Object.freeze({
    r1: 3,
    r2: 4,
    r3: 6,
});

export const MOTION = Object.freeze({
    ease: "cubic-bezier(.2,.6,.2,1)",
    dur: "150ms",
});

export const FONT = Object.freeze({
    sans: `"Inter", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif`,
    mono: `"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`,
});

export function lightenColor(hex, amount) {
    if (typeof hex !== "string" || !/^#[0-9a-fA-F]{6}$/.test(hex)) return hex;
    const mix = Math.max(0, Math.min(1, amount));
    const channel = (offset) => {
        const value = parseInt(hex.slice(offset, offset + 2), 16);
        return Math.round(value + (255 - value) * mix);
    };
    return `#${channel(1).toString(16).padStart(2, "0")}${channel(3).toString(16).padStart(2, "0")}${channel(5).toString(16).padStart(2, "0")}`;
}

export function scaleColor(hex, factor) {
    if (typeof hex !== "string" || !/^#[0-9a-fA-F]{6}$/.test(hex)) return hex;
    const scale = Math.max(0.2, Math.min(2.0, factor));
    const channel = (offset) => {
        const value = parseInt(hex.slice(offset, offset + 2), 16);
        return Math.round(Math.max(0, Math.min(255, value * scale)));
    };
    return `#${channel(1).toString(16).padStart(2, "0")}${channel(3).toString(16).padStart(2, "0")}${channel(5).toString(16).padStart(2, "0")}`;
}

export const LANE_PALETTE = Object.freeze([
    "#5d8aa0",
    "#7a8e8e",
    "#8a7fa0",
    "#6b7280",
    "#6f8c63",
    "#8b7f6b",
]);

export const EDITOR_COLORS = Object.freeze({
    bg: THEME.bg0,
    panelMuted: THEME.bg0,
    panel: THEME.bg1,
    panelRaised: THEME.bg2,
    panelRaisedHover: THEME.bg3,
    ruler: THEME.bg2,
    rulerText: THEME.fg2,
    rulerTick: THEME.line2,
    track: THEME.bg2,
    trackBorder: THEME.line2,

    clip: "#2c3a47",
    clipSelected: "#43505c",
    motionDriver: "#352f3e",
    motionDriverSelected: "#4d4655",
    audioClip: "#2f3a36",
    audioClipSelected: "#46514d",
    guide: "#435f78",
    guideSelected: "#5d7f9c",
    guideBorder: "#89a4bc",
    guideLabelBg: "rgba(12, 16, 21, 0.68)",
    promptSection: "rgba(53, 47, 62, 0.72)",
    promptSectionSelected: "rgba(77, 70, 85, 0.86)",
    promptBorder: "rgba(138, 127, 160, 0.54)",
    laneVideo: "#5d8aa0",
    laneAudio: "#7a8e8e",
    laneDriver: "#8a7fa0",
    laneGuide: "#5d7f9c",
    lanePrompt: "#8a7fa0",
    missingMedia: "#3d272c",
    missingMediaSelected: "#56353b",
    missingMediaBorder: THEME.statusFailed,
    missingMediaText: "#dfb1b1",
    selection: "rgba(102, 134, 163, 0.13)",
    selectionBorder: "rgba(102, 134, 163, 0.62)",
    selectionContext: "rgba(102, 134, 163, 0.07)",
    selectionContextBorder: "rgba(102, 134, 163, 0.24)",
    maskOffset: "rgba(138, 127, 160, 0.10)",
    maskOffsetBorder: "rgba(138, 127, 160, 0.36)",
    playhead: THEME.accentHi,
    snapIndicator: THEME.accent,

    galleryBg: THEME.bg1,
    galleryItem: THEME.bg2,
    galleryItemHover: THEME.bg3,
    galleryItemBorder: THEME.line2,
    galleryText: THEME.fg0,
    galleryLabel: THEME.fg2,
    sceneBar: THEME.bg1,
    sceneBtn: THEME.bg2,
    sceneBtnHover: THEME.bg3,
    sceneBtnActive: THEME.bg4,
    text: THEME.fg0,
    textDim: THEME.fg2,
    textMuted: THEME.fg3,
    border: THEME.line2,
    borderSoft: THEME.line1,
    borderStrong: THEME.line2,
    accent: THEME.accent,
    accentHi: THEME.accentHi,
    accentLo: THEME.accentLo,
    accentSoft: THEME.accentBg,
    accentSoftHover: THEME.accentLo,
    accentBorder: THEME.accent,
    warningSoft: "#33281c",
    warningBorder: THEME.statusPending,
    warningText: "#e8c995",
    dangerSoft: "#321f24",
    dangerBorder: THEME.statusFailed,
    dangerText: "#dfb1b1",
});

export const EDITOR_CHROME = EDITOR_COLORS;

export const CHROME_SCROLLBAR_CLASS = "sonder-chrome-scrollbar";

const BUTTON_VARIANTS = Object.freeze({
    primary: {
        background: THEME.accent,
        hoverBackground: THEME.accentHi,
        activeBackground: THEME.accentLo,
        border: THEME.accent,
        text: THEME.bg0,
        activeText: THEME.fg0,
        fontWeight: TYPE.fwBold,
    },
    secondary: {
        background: THEME.bg2,
        hoverBackground: THEME.bg3,
        activeBackground: THEME.bg4,
        border: THEME.line2,
        text: THEME.fg0,
        activeText: THEME.fg0,
        fontWeight: TYPE.fwMed,
    },
    tertiary: {
        background: "transparent",
        hoverBackground: THEME.bg2,
        activeBackground: THEME.bg3,
        border: "transparent",
        text: THEME.fg1,
        activeText: THEME.fg0,
        fontWeight: TYPE.fwMed,
    },
    muted: {
        background: THEME.bg2,
        hoverBackground: THEME.bg3,
        activeBackground: THEME.bg4,
        border: THEME.line2,
        text: THEME.fg2,
        activeText: THEME.fg0,
        fontWeight: TYPE.fwMed,
    },
    subtle: {
        background: THEME.bg1,
        hoverBackground: THEME.bg2,
        activeBackground: THEME.bg3,
        border: THEME.line2,
        text: THEME.fg2,
        activeText: THEME.fg0,
        fontWeight: TYPE.fwMed,
    },
    active: {
        background: THEME.accent,
        hoverBackground: THEME.accentHi,
        activeBackground: THEME.accentLo,
        border: THEME.accentHi,
        text: THEME.bg0,
        activeText: THEME.fg0,
        fontWeight: TYPE.fwBold,
    },
    warning: {
        background: EDITOR_COLORS.warningSoft,
        hoverBackground: scaleColor(EDITOR_COLORS.warningSoft, 1.16),
        activeBackground: scaleColor(EDITOR_COLORS.warningSoft, 1.28),
        border: EDITOR_COLORS.warningBorder,
        text: EDITOR_COLORS.warningText,
        activeText: EDITOR_COLORS.warningText,
        fontWeight: TYPE.fwMed,
    },
    danger: {
        background: EDITOR_COLORS.dangerSoft,
        hoverBackground: scaleColor(EDITOR_COLORS.dangerSoft, 1.16),
        activeBackground: scaleColor(EDITOR_COLORS.dangerSoft, 1.28),
        border: EDITOR_COLORS.dangerBorder,
        text: EDITOR_COLORS.dangerText,
        activeText: EDITOR_COLORS.dangerText,
        fontWeight: TYPE.fwMed,
    },
});

function buttonPalette(variant) {
    return BUTTON_VARIANTS[variant] || BUTTON_VARIANTS.secondary;
}

export function chromeButtonCss({
    variant = "secondary",
    padding = `${SPACE.s2}px ${SPACE.s3}px`,
    fontSize = `${TYPE.t11}px`,
    radius = `${RADIUS.r2}px`,
    lineHeight = "1.35",
    fontWeight = null,
} = {}) {
    const palette = buttonPalette(variant);
    return `
        appearance: none;
        background: ${palette.background};
        color: ${palette.text};
        border: 1px solid ${palette.border};
        border-radius: ${radius};
        padding: ${padding};
        cursor: pointer;
        font-family: ${FONT.sans};
        font-size: ${fontSize};
        font-weight: ${fontWeight || palette.fontWeight};
        line-height: ${lineHeight};
        transition: background ${MOTION.dur} ${MOTION.ease}, color ${MOTION.dur} ${MOTION.ease}, border-color ${MOTION.dur} ${MOTION.ease};
    `;
}

export function setButtonVariant(button, variant = "secondary", options = {}) {
    if (!button?.style) return button;
    const palette = buttonPalette(variant);
    button.style.cssText = chromeButtonCss({ ...options, variant });
    button.onmouseenter = () => {
        button.style.background = palette.hoverBackground;
    };
    button.onmouseleave = () => {
        button.style.background = palette.background;
        button.style.color = palette.text;
    };
    button.onmousedown = () => {
        button.style.background = palette.activeBackground;
        button.style.color = palette.activeText;
    };
    button.onmouseup = () => {
        button.style.background = palette.hoverBackground;
        button.style.color = palette.text;
    };
    return button;
}

export function chromeInputCss({ minWidth = "0", padding = "6px 8px", fontSize = `${TYPE.t11}px` } = {}) {
    return `
        background: ${THEME.bg1};
        border: 1px solid ${THEME.line2};
        border-radius: ${RADIUS.r2}px;
        color: ${THEME.fg0};
        padding: ${padding};
        font-family: ${FONT.sans};
        font-size: ${fontSize};
        min-width: ${minWidth};
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.03);
    `;
}

export function chromeSelectCss(options = {}) {
    return `${chromeInputCss(options)}cursor:pointer;`;
}

export function chromeScrollbarCss() {
    return `
        scrollbar-width: thin;
        scrollbar-color: ${THEME.fg3} ${THEME.bg0};
    `;
}

export function installChromeScrollbarStyles(targetDocument = null) {
    const doc = targetDocument || (typeof document !== "undefined" ? document : null);
    if (!doc?.head || doc.getElementById("sonder-editor-scrollbar-styles")) return;
    const styleEl = doc.createElement("style");
    styleEl.id = "sonder-editor-scrollbar-styles";
    styleEl.textContent = `
        .${CHROME_SCROLLBAR_CLASS}::-webkit-scrollbar {
            width: 10px;
            height: 10px;
        }
        .${CHROME_SCROLLBAR_CLASS}::-webkit-scrollbar-track {
            background: ${THEME.bg0};
            border-left: 1px solid ${THEME.line1};
        }
        .${CHROME_SCROLLBAR_CLASS}::-webkit-scrollbar-thumb {
            background: ${THEME.fg3};
            border: 2px solid ${THEME.bg0};
            border-radius: 999px;
        }
        .${CHROME_SCROLLBAR_CLASS}::-webkit-scrollbar-thumb:hover {
            background: ${THEME.accent};
        }
        .${CHROME_SCROLLBAR_CLASS}::-webkit-scrollbar-corner {
            background: ${THEME.bg0};
        }
    `;
    doc.head.appendChild(styleEl);
}

export function statusPillCss({ state = "idle", padding = "4px 8px" } = {}) {
    const color = {
        idle: THEME.statusIdle,
        running: THEME.statusRunning,
        completed: THEME.statusCompleted,
        pending: THEME.statusPending,
        failed: THEME.statusFailed,
        progress: THEME.accent,
    }[state] || THEME.statusIdle;
    const glow = state === "running"
        ? `box-shadow:0 0 0 1px ${THEME.statusRunning}44;`
        : state === "progress"
            ? `box-shadow:0 0 0 1px ${THEME.accent}44;`
            : "";
    return `
        display: inline-flex;
        align-items: center;
        gap: 6px;
        background: ${THEME.bg2};
        border: 1px solid ${THEME.line2};
        border-radius: ${RADIUS.r3}px;
        color: ${THEME.fg1};
        padding: ${padding};
        font-family: ${FONT.sans};
        font-size: ${TYPE.t11}px;
        --sonder-status-color: ${color};
        ${glow}
    `;
}

export function tabCss({ active = false } = {}) {
    return `
        background: ${active ? THEME.bg4 : "transparent"};
        border: 1px solid ${active ? THEME.line2 : "transparent"};
        border-radius: ${RADIUS.r2}px;
        color: ${active ? THEME.fg0 : THEME.fg1};
        padding: 5px 8px;
        font-family: ${FONT.sans};
        font-size: ${TYPE.t11}px;
        font-weight: ${TYPE.fwMed};
    `;
}

export function segmentCss({ active = false } = {}) {
    return `
        background: ${active ? THEME.accentBg : THEME.bg2};
        border: 1px solid ${active ? THEME.accent : THEME.line2};
        color: ${active ? THEME.fg0 : THEME.fg1};
        padding: 4px 8px;
        font-family: ${FONT.sans};
        font-size: ${TYPE.t11}px;
        font-weight: ${TYPE.fwMed};
    `;
}

export function chromeMenuCss(minWidth = 160) {
    return `
        position: fixed;
        z-index: 10000;
        background: ${THEME.bg2};
        border: 1px solid ${THEME.line2};
        border-radius: ${RADIUS.r3}px;
        box-shadow: 0 12px 28px rgba(0,0,0,0.42);
        min-width: ${minWidth}px;
        padding: 6px 0;
        font-family: ${FONT.sans};
        font-size: ${TYPE.t11}px;
    `;
}

export function chromeOverlayPanelCss({ width = "90%", maxWidth = "520px", maxHeight = "80vh", padding = "20px 28px" } = {}) {
    return `
        background: ${THEME.bg1};
        border: 1px solid ${THEME.line2};
        border-radius: ${RADIUS.r3}px;
        padding: ${padding};
        width: ${width};
        max-width: ${maxWidth};
        max-height: ${maxHeight};
        overflow-y: auto;
        color: ${THEME.fg0};
        font-family: ${FONT.sans};
        font-size: ${TYPE.t12}px;
        box-shadow: 0 24px 60px rgba(0,0,0,0.46);
    `;
}

export function chromeDividerCss(height = 16) {
    return `width:1px;height:${height}px;background:${THEME.line2};margin:0 4px;`;
}

const FONT_FACE_ID = "sonder-editor-font-faces";

function fontUrl(baseUrl, fileName) {
    return new URL(fileName, baseUrl).href;
}

export function fontFaceCss(baseUrl = new URL("../fonts/", import.meta.url)) {
    const faces = [
        ["Inter", "Inter-Regular.woff2", 400],
        ["Inter", "Inter-Medium.woff2", 500],
        ["Inter", "Inter-SemiBold.woff2", 600],
        ["JetBrains Mono", "JetBrainsMono-Regular.woff2", 400],
        ["JetBrains Mono", "JetBrainsMono-Medium.woff2", 500],
    ];
    return faces.map(([family, fileName, weight]) => `
@font-face {
    font-family: "${family}";
    src: url("${fontUrl(baseUrl, fileName)}") format("woff2");
    font-weight: ${weight};
    font-style: normal;
    font-display: swap;
}`).join("\n");
}

export function injectSonderFontFaces(baseUrl = new URL("../fonts/", import.meta.url), doc = globalThis.document) {
    if (!doc?.head || doc.getElementById(FONT_FACE_ID)) return null;
    const style = doc.createElement("style");
    style.id = FONT_FACE_ID;
    style.textContent = fontFaceCss(baseUrl);
    doc.head.appendChild(style);
    return style;
}
