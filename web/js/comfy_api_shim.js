export function installComfyApiShim() {
    const mountedPrefix = (() => {
        const pathname = window.location?.pathname || "";
        const marker = "/sonder-editor";
        const markerIndex = pathname.indexOf(marker);
        return markerIndex > 0 ? pathname.slice(0, markerIndex) : "";
    })();

    const api = {
        apiURL(path) {
            if (!path) return "";
            if (/^(https?|wss?):\/\//i.test(path)) return path;
            const normalizedPath = path.startsWith("/") ? path : `/${path}`;
            if (
                mountedPrefix &&
                normalizedPath.startsWith("/sonder-editor") &&
                !normalizedPath.startsWith(`${mountedPrefix}/sonder-editor`)
            ) {
                return `${mountedPrefix}${normalizedPath}`;
            }
            return normalizedPath;
        },
        addEventListener() {},
        removeEventListener() {},
    };

    const app = {
        graph: null,
        canvas: {
            setDirty() {},
        },
        registerExtension() {},
    };

    window.comfyAPI = window.comfyAPI || {};
    window.comfyAPI.api = window.comfyAPI.api || { api };
    window.comfyAPI.api.api = window.comfyAPI.api.api || api;
    window.comfyAPI.app = window.comfyAPI.app || { app };
    window.comfyAPI.app.app = window.comfyAPI.app.app || app;
}
