import { app } from "/scripts/app.js";

const EXT_NAME = "sonder.selector";
const TARGET_SELECTOR = "SonderSelector";

const arraysEqual = (left, right) => {
    if (left.length !== right.length) return false;
    for (let index = 0; index < left.length; index += 1) {
        if (left[index] !== right[index]) return false;
    }
    return true;
};

const parseChoiceList = (text) =>
    String(text || "")
        .split(/\r?\n/)
        .map((value) => value.trim())
        .filter(Boolean);

app.registerExtension({
    name: EXT_NAME,

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== TARGET_SELECTOR) return;

        const originalOnNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = originalOnNodeCreated?.apply(this, arguments);
            const node = this;
            const choiceWidget = node.widgets?.find((widget) => widget?.name === "choice");
            const listWidget = node.widgets?.find((widget) => widget?.name === "choice_list");
            if (!choiceWidget || !listWidget) return result;

            let previousItems = [];

            // Dynamic combo behavior adapted from wakaura-asaho/comfyui-dynamic-selector.
            const updateCombo = () => {
                const items = parseChoiceList(listWidget.value);
                if (arraysEqual(items, previousItems)) return;

                previousItems = [...items];
                choiceWidget.options ||= {};
                choiceWidget.options.values = items.length ? items : [""];

                if (!items.includes(choiceWidget.value)) {
                    choiceWidget.value = items[0] || "";
                }

                choiceWidget.callback?.call(choiceWidget, choiceWidget.value);
                app.graph?.setDirtyCanvas?.(true, true);
            };

            const originalListCallback = listWidget.callback;
            listWidget.callback = function () {
                const callbackResult = originalListCallback?.apply(this, arguments);
                updateCombo();
                return callbackResult;
            };

            requestAnimationFrame(updateCombo);
            return result;
        };
    },
});
