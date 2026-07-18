/**
 * Scorpiov Image Meta Reader — ComfyUI UI Extension
 *
 * Displays all image metadata in a single scrollable textarea — model, VAE,
 * LoRAs with weights, positive prompt, negative prompt, and the complete raw
 * metadata dump (so ADetailer, Hires fix, sampler settings, etc. are all visible).
 *
 * Two ways to populate:
 *   1. Run the workflow — the "executed" websocket event fires and fills the box.
 *   2. Click "🔍 Read Metadata Now" — hits the REST endpoint directly, no generation needed.
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

function getWidget(node, name) {
    return node.widgets?.find((w) => w.name === name);
}

// ── Build the single-textarea metadata panel ─────────────────────────────────
function addMetaPanel(node) {
    const container = document.createElement("div");
    container.style.cssText = [
        "width: 100%",
        "box-sizing: border-box",
        "padding: 2px 0 0 0",
    ].join(";");

    // Status line — shows parse format or error
    const statusBar = document.createElement("div");
    statusBar.textContent = "Run the workflow (or click Read Metadata Now) to populate.";
    statusBar.style.cssText = [
        "font-size: 10px",
        "color: #6b7280",
        "font-style: italic",
        "margin-bottom: 4px",
        "font-family: sans-serif",
        "user-select: none",
    ].join(";");
    container.appendChild(statusBar);

    // The single large textarea that holds everything
    const textarea = document.createElement("textarea");
    textarea.readOnly = true;
    textarea.rows = 20;
    textarea.placeholder = "(metadata will appear here)";
    textarea.style.cssText = [
        "width: 100%",
        "box-sizing: border-box",
        "background: #0f172a",
        "color: #e2e8f0",
        "border: 1px solid #334155",
        "border-radius: 4px",
        "padding: 8px 10px",
        "font-size: 11px",
        "font-family: 'Consolas', 'Courier New', monospace",
        "line-height: 1.6",
        "resize: vertical",
        "cursor: default",
        "outline: none",
        "white-space: pre-wrap",
        "overflow-wrap: break-word",
        "overflow-x: auto",
        "overflow-y: auto",
    ].join(";");

    // Stop ComfyUI from stealing key/mouse events while the user scrolls or selects text
    textarea.addEventListener("keydown",   (e) => e.stopPropagation());
    textarea.addEventListener("mousedown", (e) => e.stopPropagation());
    textarea.addEventListener("wheel",     (e) => e.stopPropagation());

    container.appendChild(textarea);

    node.addDOMWidget("meta_display", "customtext", container, {
        getValue() { return textarea.value; },
        setValue(v) { textarea.value = v ?? ""; },
        serialize: false,
    });

    node._scorpiovMeta = { statusBar, textarea };

    // Start tall enough to be useful
    node.setSize([node.size[0], Math.max(node.size[1], 540)]);
}

// ── Fill the textarea with formatted text ────────────────────────────────────
function setDisplay(node, text, statusText = "", isError = false) {
    const p = node._scorpiovMeta;
    if (!p) return;

    p.textarea.value = text || "";
    p.statusBar.textContent = statusText || "✅ Done";
    p.statusBar.style.color = isError ? "#f87171" : "#34d399";

    // Auto-grow node height to show content without scrolling (capped at +400px)
    const lineCount   = (text.match(/\n/g) ?? []).length + 2;
    const textHeight  = Math.min(lineCount * 17, 500);
    const desiredH    = 300 + textHeight;
    if (node.size[1] < desiredH) {
        node.setSize([node.size[0], desiredH]);
    }

    node.setDirtyCanvas(true, true);
}

// ── Extension ────────────────────────────────────────────────────────────────
app.registerExtension({
    name: "Scorpiov.ImageMetaReader",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "ScorpiovImageMeta") return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;

        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated?.apply(this, arguments);
            const node   = this;

            // ── READ METADATA NOW BUTTON ─────────────────────────────────
            node.addWidget(
                "button",
                "🔍 Read Metadata Now",
                null,
                async () => {
                    const btn        = getWidget(node, "🔍 Read Metadata Now");
                    const pathWidget = getWidget(node, "image_path");
                    const imagePath  = pathWidget?.value?.trim() ?? "";

                    if (!imagePath) {
                        setDisplay(node, "", "❌ Enter an image path first.", true);
                        return;
                    }

                    if (btn) btn.name = "⏳ Reading...";
                    node.setDirtyCanvas(true, true);

                    try {
                        const response = await fetch("/scorpiov/imagemeta/read", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ path: imagePath }),
                        });

                        const data = await response.json();

                        if (data.status === "ok") {
                            const meta   = data.meta;
                            // Backend now sends a pre-built display_text — just use it
                            const text   = meta.display_text || "(no display text returned)";
                            const format = meta.format || "unknown";
                            setDisplay(node, text, `✅ Parsed  ·  format: ${format}  ·  ${imagePath.split(/[\\/]/).pop()}`);
                        } else {
                            setDisplay(node, "", `❌ ${data.message || "Unknown error"}`, true);
                        }
                    } catch (err) {
                        setDisplay(node, "", "❌ Fetch failed — check browser console.", true);
                        console.error("[Scorpiov Meta] Read error:", err);
                    }

                    setTimeout(() => {
                        if (btn) btn.name = "🔍 Read Metadata Now";
                        node.setDirtyCanvas(true, true);
                    }, 2000);
                },
                { serialize: false }
            );

            // ── SINGLE METADATA TEXTAREA ─────────────────────────────────
            addMetaPanel(node);

            return result;
        };
    },

    async setup() {
        // ── Populate after a workflow run ────────────────────────────────
        // The backend puts the pre-formatted display string in
        // ui.meta_display[0] — we just slam it straight into the textarea.
        api.addEventListener("executed", (event) => {
            const detail = event.detail;
            if (!detail?.output?.meta_display) return;

            const nodeId      = parseInt(detail.node);
            const displayText = detail.output.meta_display[0];
            if (typeof displayText !== "string") return;

            const node = app.graph.getNodeById(nodeId);
            if (!node || node.comfyClass !== "ScorpiovImageMeta") return;

            setDisplay(node, displayText, "✅ Metadata read from workflow run.");
        });
    },
});
