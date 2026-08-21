# 😺NKD Sigmas Curve

Draw the noise schedule itself and feed it to any sampler.

## Why this exists

It’s all about control. Standard schedulers (Karras, exponential, etc.) give you a fixed curve shape (nothing wrong with that) but once you unlock the power of custom sigmas, you can decide exactly how you want to denoise the image. That gives you fine-grained control over composition, details, and a bunch of nerdy stuff.

https://github.com/user-attachments/assets/281fa043-0900-4e7b-883d-1018953b01e0

This is all with a fixed seed. As you can see, specially in the las 2 generations. Tuning the sigma curve lets you nail the shapes and details at just the right moment during generation. For instance, I use it to swap out a bare chest for a T-shirt on the fly.


## How it works / How to use it

- I **strongly, highly, super recommend using it alongside the [RES4LYF](https://github.com/ClownsharkBatwing/RES4LYF.git)** node pack (and joining the bongmath cult), but technically you could plug it into any sigmas input, like in a _CustomSampler_. 
- The node overrides the scheduler and steps, so set the **Ksampler to 1.0 denoise and control these from the Sigmas Curves node instead**.
- If you know nothing about sigmas, treat the _max_sigma_ value as your new "denoise" setting (kind of).
- The curve is your new "scheduler" (you're basically drawing it yourself instead of picking one from a dropdown).
- You can choose between linear curve or b-spline type. Up to you.

## Features

- **Interactive canvas widget** embedded directly in the ComfyUI node, no external tools needed
- **Click** to add control points, **drag** to reposition, **Shift+click** to remove
- **Two interpolation modes:**
  - **Smooth** — B-spline with tension weights
  - **Linear** — Piecewise linear between control points
- Outputs a standard `SIGMAS` tensor compatible with **all ComfyUI samplers**
- No extra Python dependencies beyond what ComfyUI already includes

---

[← All 😺NKD Sigmas Curve nodes](../README.md)
