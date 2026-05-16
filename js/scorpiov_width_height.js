/**
 * Scorpiov Width Height Node — ComfyUI UI Extension
 *
 * - Greys out the width and height fields whenever a preset ratio is selected
 *   (they only activate when "Custom" is chosen)
 * - Adds a Refresh button that reloads ratios.txt via POST /scorpiov/wh/refresh
 *   without triggering generation
 */

import { app } from "../../scripts/app.js";

function getWidget(node, name) {
    return node.widgets?.find((w) => w.name === name);
}

app.registerExtension({
    name: "Scorpiov.WidthHeight",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "ScorpiovWidthHeight") return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;

        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated?.apply(this, arguments);
            const node = this;

            // ── Grey out width/height when a preset is active ────────────
            // We watch the ratio widget and toggle disabled styling on the
            // width and height widgets whenever it changes.
            const applyCustomState = () => {
                const ratioWidget  = getWidget(node, "ratio");
                const widthWidget  = getWidget(node, "width");
                const heightWidget = getWidget(node, "height");
                if (!ratioWidget || !widthWidget || !heightWidget) return;

                const isCustom = ratioWidget.value === "Custom";

                // ComfyUI number widgets expose an inputEl DOM element
                for (const w of [widthWidget, heightWidget]) {
                    if (w.inputEl) {
                        w.inputEl.disabled = !isCustom;
                        w.inputEl.style.opacity  = isCustom ? "1"       : "0.35";
                        w.inputEl.style.cursor   = isCustom ? "text"    : "not-allowed";
                        w.inputEl.title          = isCustom
                            ? ""
                            : "Set ratio to Custom to edit this value";
                    }
                }
            };

            // Run once on creation, then hook into ratio changes
            const hookRatioWidget = () => {
                const ratioWidget = getWidget(node, "ratio");
                if (!ratioWidget) return;

                applyCustomState(); // initial state

                const origCallback = ratioWidget.callback;
                ratioWidget.callback = function (...args) {
                    origCallback?.apply(this, args);
                    applyCustomState();
                };
            };

            // Widgets may not be ready immediately on creation
            setTimeout(hookRatioWidget, 50);

            // ── REFRESH BUTTON ───────────────────────────────────────────
            // Calls POST /scorpiov/wh/refresh — reloads ratios.txt on the
            // backend. Does NOT trigger generation.
            // NOTE: The dropdown won't show new entries until ComfyUI is
            // restarted (ComfyUI bakes dropdown options at load time), but
            // the backend will use updated values immediately.
            node.addWidget(
                "button",
                "🔄 Refresh Ratios",
                null,
                async () => {
                    const btn = getWidget(node, "🔄 Refresh Ratios");
                    if (btn) btn.name = "⏳ Refreshing...";
                    node.setDirtyCanvas(true, true);

                    try {
                        const res  = await fetch("/scorpiov/wh/refresh", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({}),
                        });
                        const data = await res.json();

                        if (data.status === "ok") {
                            const count = data.ratios?.length ?? 0;
                            if (btn) btn.name = `✅ ${count} ratios loaded (restart to update dropdown)`;
                        } else {
                            if (btn) btn.name = "❌ Refresh failed";
                        }
                    } catch (err) {
                        if (btn) btn.name = "❌ Error (check console)";
                        console.error("[Scorpiov WH] Refresh error:", err);
                    }

                    setTimeout(() => {
                        if (btn) btn.name = "🔄 Refresh Ratios";
                        node.setDirtyCanvas(true, true);
                    }, 4000);
                },
                { serialize: false }
            );

            return result;
        };
    },
});
