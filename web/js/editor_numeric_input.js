export function evalNumericExpression(source) {
    const text = String(source ?? "").trim();
    if (!text) return NaN;

    let index = 0;

    const skipSpace = () => {
        while (index < text.length && /\s/.test(text[index])) index += 1;
    };

    const parseNumber = () => {
        skipSpace();
        const start = index;
        while (index < text.length && /[0-9]/.test(text[index])) index += 1;
        if (text[index] === ".") {
            index += 1;
            while (index < text.length && /[0-9]/.test(text[index])) index += 1;
        }
        if (index === start || text.slice(start, index) === ".") return NaN;
        return Number(text.slice(start, index));
    };

    const parseFactor = () => {
        skipSpace();
        const char = text[index];
        if (char === "+" || char === "-") {
            index += 1;
            const value = parseFactor();
            return char === "-" ? -value : value;
        }
        if (char === "(") {
            index += 1;
            const value = parseExpression();
            skipSpace();
            if (text[index] !== ")") return NaN;
            index += 1;
            return value;
        }
        return parseNumber();
    };

    const parseTerm = () => {
        let value = parseFactor();
        while (Number.isFinite(value)) {
            skipSpace();
            const op = text[index];
            if (op !== "*" && op !== "/") break;
            index += 1;
            const rhs = parseFactor();
            if (!Number.isFinite(rhs)) return NaN;
            value = op === "*" ? value * rhs : value / rhs;
        }
        return value;
    };

    function parseExpression() {
        let value = parseTerm();
        while (Number.isFinite(value)) {
            skipSpace();
            const op = text[index];
            if (op !== "+" && op !== "-") break;
            index += 1;
            const rhs = parseTerm();
            if (!Number.isFinite(rhs)) return NaN;
            value = op === "+" ? value + rhs : value - rhs;
        }
        return value;
    }

    const result = parseExpression();
    skipSpace();
    return index === text.length && Number.isFinite(result) ? result : NaN;
}
