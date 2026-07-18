/**
 * Scorpiov Save Image — ComfyUI UI Extension
 *
 * Adds a preview panel below the save node that shows:
 *   - The full path of the last saved file
 *   - A thumbnail of the saved image (uses ComfyUI's built-in image URL)
 *
 * Also shows a small status bar confirming metadata was embedded.
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

app.registerExtension({
    name: "Scorpiov.SaveImage",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "ScorpiovSaveImage") return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;

        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated?.apply(this, arguments);
            const node   = this;

            // Guard: onNodeCreated can fire more than once on the same node
            // (e.g. after an error, or certain UI refreshes). Without this
            // guard, a second status widget gets added each time it fires,
            // silently shifting every widget after it out of position.
            if (node._scorpiovSave) return result;

            // ── Status / last-saved display ──────────────────────────────
            const container = document.createElement("div");
            container.style.cssText = [
                "width: 100%",
                "box-sizing: border-box",
                "padding: 4px 0 0 0",
                "font-family: monospace",
            ].join(";");

            const statusBar = document.createElement("div");
            statusBar.textContent = "No image saved yet.";
            statusBar.style.cssText = [
                "font-size: 10px",
                "color: #6b7280",
                "font-style: italic",
                "margin-bottom: 4px",
                "font-family: sans-serif",
                "user-select: none",
            ].join(";");

            const pathField = document.createElement("input");
            pathField.type     = "text";
            pathField.readOnly = true;
            pathField.placeholder = "(saved file path will appear here)";
            pathField.style.cssText = [
                "width: 100%",
                "box-sizing: border-box",
                "background: #0f172a",
                "color: #93c5fd",
                "border: 1px solid #334155",
                "border-radius: 3px",
                "padding: 3px 6px",
                "font-size: 11px",
                "font-family: monospace",
                "outline: none",
                "margin-bottom: 4px",
            ].join(";");
            pathField.addEventListener("mousedown", (e) => e.stopPropagation());

            container.appendChild(statusBar);
            container.appendChild(pathField);

            const statusWidget = node.addDOMWidget("save_status", "customtext", container, {
                getValue() { return pathField.value; },
                setValue(v) {},
                serialize: false,
            });
            // Belt-and-suspenders: some ComfyUI frontend versions don't fully
            // respect serialize:false passed via options, so set it directly
            // on the widget object too.
            statusWidget.serialize = false;

            node._scorpiovSave = { statusBar, pathField };

            return result;
        };
    },

    async setup() {
        // ── After a successful save, update the status display ───────────
        api.addEventListener("executed", (event) => {
            const detail = event.detail;
            if (!detail?.output?.images) return;

            const nodeId = parseInt(detail.node);
            const node   = app.graph.getNodeById(nodeId);
            if (!node || node.comfyClass !== "ScorpiovSaveImage") return;

            const p = node._scorpiovSave;
            if (!p) return;

            const saved = detail.output.images;
            if (!saved?.length) return;

            // Show the last saved filename
            const last = saved[saved.length - 1];
            p.pathField.value       = last.filename || "(unknown)";
            p.statusBar.textContent = `💾 Saved ${saved.length} image${saved.length > 1 ? "s" : ""} with metadata embedded`;
            p.statusBar.style.color = "#34d399";

            node.setDirtyCanvas(true, true);
        });
    },
});
