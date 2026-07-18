# scorpiov-nodes
<img width="786" height="710" alt="Screenshot 2026-05-16 115615" src="https://github.com/user-attachments/assets/39fe0215-2d6f-4a6c-ab8b-69056b8d9166" />

A collection of nodes that I use in comfyui. Based on nodes used in the past with some tweaks.

**WildCard Processor**
<img width="762" height="789" alt="workflow (2)" src="https://github.com/user-attachments/assets/669292e9-5bd0-4601-a34e-cf0153378e40" />

All-in-one wildcard processor with LoRA loading, serial/random modes, and prompt preview.
- Put your wildcard txt files into the wildcards folder in the node folder

**Set Image Size/Ratio**
<img width="588" height="426" alt="workflow" src="https://github.com/user-attachments/assets/832fa349-aa3a-480d-bed5-3348424df556" />

Aspect Ratio Node, set width/height for empty latents or choose from a list
- Add to the list by updating the ratio.txt file in the node folder
- - Eg: My Custom Ratio (2:1) | 2048 | 1024

# scorpiov-nodes

A custom node pack for [ComfyUI](https://github.com/comfyanonymous/ComfyUI) focused on prompt wildcards, image metadata (A1111 / Civitai compatible), and quality-of-life save/load utilities.

## Installation

1. Download or clone this repository into your ComfyUI custom nodes folder:
   ```
   ComfyUI/custom_nodes/scorpiov-nodes/
   ```
2. Restart ComfyUI. The nodes will appear under the **Scorpiov** category in the node search/menu.
3. (Optional) Drop your own wildcard `.txt` files into the `wildcards/` folder — any subfolder depth is supported, no configuration needed.

No extra Python dependencies beyond what ComfyUI already provides.

## Nodes

### 🎲 Scorpiov Wildcard Processor
`Scorpiov/Prompt`

All-in-one wildcard resolver that also produces ready-to-use conditioning. Type a prompt containing `__filename__` wildcard references and/or inline `{option a|option b|option c}` groups, and it resolves them, loads any `<lora:name:weight>` tags it finds directly onto the given model/clip, and encodes the result.

| Inputs | Outputs |
| --- | --- |
| `model`, `clip` | `conditioning` |
| `text` (prompt with wildcards) | `model` (updated if LoRAs were loaded) |
| `mode`: `random` or `serial` | `clip` (updated if LoRAs were loaded) |
| `seed` | `processed_text` |
| `serial_start_line` | |

- **random** mode picks a new random option each run; **serial** mode steps through wildcard file lines in order, starting from `serial_start_line`.
- Automatically re-runs every generation (rather than caching) so a new random pick is made each time.

### 📝 Scorpiov Wildcard Prompter
`Scorpiov/Prompt`

Text-only sibling of the Wildcard Processor. Same wildcard engine (`__file__` references, `{a|b|c}` groups, random/serial modes), but with no `model`/`clip` inputs and no LoRA loading — it simply outputs the resolved prompt string. Useful for feeding text into other nodes (e.g. **Scorpiov Save Image**) without needing a conditioning pipeline at that point in the graph.

| Inputs | Outputs |
| --- | --- |
| `text`, `mode`, `seed`, `serial_start_line` | `processed_text` |

### 📐 Scorpiov Width Height
`Scorpiov/Image`

Resolution helper. Pick a ratio preset (or Custom), apply an upscale multiplier, and get back matching width/height values plus a ready-to-use empty latent — all three wirable independently.

| Inputs | Outputs |
| --- | --- |
| `ratio` (preset list, or Custom) | `width` |
| `width`, `height` (used when ratio = Custom) | `height` |
| `upscale_factor` (multiplies both dimensions) | `empty_latent` |
| `batch_size` | |

Output dimensions are automatically rounded to the nearest multiple of 8, as required by Stable Diffusion.

### 🔍 Scorpiov Image Meta Reader
`Scorpiov/Image`

Reads embedded generation metadata from a PNG and breaks it out into separate, wirable fields. Supports both major metadata formats:

- **A1111 / Automatic1111** — parses the `parameters` text chunk (steps, sampler, model, VAE, LoRAs, prompts, etc.)
- **ComfyUI native** — parses the `prompt` JSON chunk by walking the embedded workflow graph (`CheckpointLoaderSimple`, `VAELoader`, `LoraLoader`, `KSampler` → `CLIPTextEncode`, etc.)

The node auto-detects which format is present and parses accordingly.

| Inputs | Outputs |
| --- | --- |
| `image_path` (filename from a Load Image node, or a full path) | `model`, `vae`, `loras`, `positive_prompt`, `negative_prompt`, `raw_metadata` |

Includes a **Read Metadata Now** button in the UI (backed by a `/scorpiov/imagemeta/read` endpoint) to preview metadata instantly without running the full workflow.

### 🖼️ Scorpiov Image Loader
`Scorpiov/Image`

Loads an image from your ComfyUI `input` folder and outputs the tensor alongside its filename and full path — handy when downstream nodes (like the Image Meta Reader) need the path as text rather than just the image tensor.

| Inputs | Outputs |
| --- | --- |
| `image` (upload or pick from input folder) | `image`, `filename`, `file_path` |

### 💾 Scorpiov Save Image
`Scorpiov/Image`

Saves images with Civitai/A1111-compatible metadata embedded in the PNG. Auto-detects generation settings directly from the workflow graph, so in the simplest case you only need to wire three things.

| Required | Optional (auto-detected from your KSampler / loaders if left blank or 0) |
| --- | --- |
| `images` | `loras`, `model_name`, `vae_name` |
| `filename_prefix` | `steps`, `cfg`, `sampler_name`, `scheduler`, `seed` |
| `positive_prompt` *(must be wired — see below)* | `save_metadata` (uncheck to save a clean PNG with no metadata) |
| `negative_prompt` *(must be wired — see below)* | |

**Notes:**
- `positive_prompt` / `negative_prompt` must come from a wired connection (e.g. **Scorpiov Wildcard Prompter** → `processed_text`) — they are not typing fields.
- `filename_prefix` supports subfolders via `/` and a `%date:yyyy-MM-dd%` token (also supports `yy`, `MM`, `dd`, `HH`, `mm`, `ss`), e.g. `%date:yyyy-MM-dd%/MyRun` → `output/2026-07-18/MyRun_00001_.png`.
- Any optional field you fill in manually overrides the auto-detected value from the workflow graph.
- LoRA tags are written into the saved prompt as `<lora:name:weight>` so Civitai displays them with badges.

## File Structure

```
scorpiov-nodes/
├── __init__.py                   ← registers all nodes + WEB_DIRECTORY
├── scorpiov_wildcard.py           ← Wildcard Processor + Wildcard Prompter
├── scorpiov_width_height.py       ← Width Height selector
├── scorpiov_image_meta.py         ← Image Meta Reader + REST endpoint
├── scorpiov_image_loader.py       ← Image Loader
├── scorpiov_save_image.py         ← Save Image with metadata
├── wildcards/                     ← put your own .txt wildcard files here (any subfolder depth)
└── js/
    ├── scorpiov_wildcard.js
    ├── scorpiov_image_meta.js
    ├── scorpiov_image_loader.js
    └── scorpiov_save_image.js
```

## License

GPL-3.0 — see [LICENSE](LICENSE).
