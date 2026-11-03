// Whole-number stepping for Controls: the INT input's declared step and the
// seed's up and down buttons. Seeds travel as decimal strings (a 64-bit seed
// never becomes a JS number), so the seed arithmetic is exact BigInt.

const WHOLE = /^-?\d+$/;
const POSITIVE = /^[1-9]\d*$/;
export const SEED_MAX = "18446744073709551615"; // 0xFFFFFFFFFFFFFFFF, server/control_descriptors.SEED_MAX
// A canvas widget holds a JS number and refuses anything larger
// (controls_host_canvas.js), so canvas hosts step no higher than this.
export const CANVAS_INTEGER_CEILING = String(Number.MAX_SAFE_INTEGER);

const text = value => String(value ?? "").trim();
const whole = value => (WHOLE.test(text(value)) ? BigInt(text(value)) : null);

// The INT input's `step` attribute: the declared step when it is a positive
// whole number, otherwise 1.
export function intStep(spec) {
    const step = text(spec?.step);
    return POSITIVE.test(step) ? step : "1";
}

// The bounds a seed steps within: its own min and max, capped by the host's
// exact-integer ceiling (none on a host that keeps big integers exact).
export function seedBounds(spec, ceiling = CANVAS_INTEGER_CEILING) {
    const lo = whole(spec?.min) ?? 0n;
    let hi = whole(spec?.max) ?? BigInt(SEED_MAX);
    const cap = whole(ceiling);
    if (cap !== null && cap < hi) hi = cap;
    const step = POSITIVE.test(text(spec?.step)) ? BigInt(text(spec.step)) : 1n;
    return { lo, hi, step };
}

// The seed after one step up (+1) or down (-1), as a decimal string, or null
// when the text is not a whole number or the seed already sits at that bound.
export function stepSeed(value, direction, bounds) {
    const current = whole(value);
    if (current === null || !bounds) return null;
    // Never step the wrong way: a seed already past a bound only moves back in.
    if (direction > 0 ? current >= bounds.hi : current <= bounds.lo) return null;
    let next = current + (direction > 0 ? bounds.step : -bounds.step);
    if (next > bounds.hi) next = bounds.hi;
    if (next < bounds.lo) next = bounds.lo;
    return next === current ? null : next.toString();
}
