# ComfyUI NKD Sigmas Curve

A ComfyUI custom node that replaces trial-and-error sigma tuning with a visual, interactive spline editor. Design your diffusion noise schedule exactly the way you want it, then plug it straight into any sampler.

The pack ships two nodes, both driven by the same curve editor:

| Node | What it does |
|---|---|
| [😺NKD Sigmas Curve](docs/sigmas-curve.md) | Draw the noise schedule itself and feed it to any sampler. |
| [😺NKD H3 Audio Shift Curve](docs/h3-audio-shift-curve.md) | MiniMax H3 only: shape how the audio stream's sigma shift moves across the run. |

---

## Installation

### Via ComfyUI Manager *(recommended)*

Search for **NKD Sigmas Curve** in the ComfyUI Manager and install with one click.

### Manual

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/Nekodificador/ComfyUI-NKD-Sigmas-Curve
```

No additional Python dependencies required. Restart ComfyUI after installing.

> **Note:** The JavaScript widget (`web/nkd_sigma_curve.js`) is pre-built and ready to use. If you want to modify the Vue source, see [Development](#development) below.

## Requirements

- ComfyUI (V3 API / Nodes 2.0 compatible)
- Python 3.10 or higher
- PyTorch (included with ComfyUI)

## Development

The widget is written in **Vue 3 + TypeScript** and bundled with Vite.

```bash
cd ComfyUI/custom_nodes/nkd_sigma_curve
npm install
npm run build   # outputs to web/nkd_sigma_curve.js
npm run dev     # watch mode
```

## Inspired by
[Custom Sigma Editor](https://github.com/JoeNavark/comfyui_custom_sigma_editor.git)

## License

MIT, use it, modify it, share it freely.

[Changelog](docs/changelog.md)
