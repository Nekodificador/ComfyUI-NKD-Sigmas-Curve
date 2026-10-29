# Changelog

### v1.5.1 · License

The pack is now GPL-3.0, the same license as ComfyUI. Releases up to v1.5.0 stay MIT.

### v1.5.0 · Calibrate + Equalize

Two new nodes that measure a noise schedule from your own model instead of picking one from a list.

- **Sigmas Calibrate** runs one reference pass and measures where the image is actually changing
- **Sigmas Equalize** spreads your steps over that, from the stock scheduler (`equalize` 0) to every step doing the same amount of work (`equalize` 1)
- Works with any model, including the newer ones that ship no Align Your Steps table
- Calibrate stays cached while you tune `steps` and `equalize`
- Calibrate also outputs the reference result as a latent and, with a VAE, an image to compare your final render against
- Wire the result into Sigmas Curve's `reference_sigmas` and press **Match** to keep editing it by hand

### v1.3.0 — Reference overlay

You can now connect any SIGMAS output (from a scheduler, another curve node, anything) to the new **reference_sigmas** input. Once you run the node, a ghost curve appears on the canvas so you can compare your design against the reference at a glance.

- **Show / Hide** — toggle the reference overlay on and off
- **Match** — copy the reference shape into your curve as editable control points, so you can use it as a starting point and tweak from there
- The overlay appears automatically as soon as the node executes with a reference connected
- Point tooltips now show the exact step and σ value while hovering or dragging
- `max_sigma` widget precision increased to 3 decimal places


https://github.com/user-attachments/assets/b91c680c-544b-4720-a5a1-1b43a5a807f6

### v1.2.0 — Snap to steps & progress dot

Now it shows the progress in real time, so you can make better decisions 🫡

https://github.com/user-attachments/assets/42c3c7af-4d89-43c1-bb6d-d623547c8e5d

---

[← All 😺NKD Sigmas Curve nodes](../README.md)
