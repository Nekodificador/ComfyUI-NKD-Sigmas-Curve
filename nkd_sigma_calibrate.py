"""
NKD Sigmas Calibrate / Equalize — derive a sigma schedule from how the model
actually denoises.

Calibrate runs one long reference pass (Euler, ``calib_steps`` steps on a stock
scheduler) and records how much the predicted final image (x0) moves between
consecutive steps. Equalize then redistributes the steps so each one does a
comparable share of that movement: dense where the image is changing fast,
sparse where little happens. Two nodes so the expensive pass stays cached while
the cheap one is tuned.

``equalize`` is the knob between the two ends:
  0 → the reference scheduler resampled to ``steps`` (nothing changes)
  1 → every step moves x0 by the same amount (equal arc length of the x0 path,
      which is independent of the reference schedule)
In between, the step density is the per-step change raised to that power.

Why not always 1: the change is measured as latent RMS, where fine detail at
low sigma is small next to the early composition moves, so full equalisation
starves the tail. Start around 0.5 and compare.

Model-agnostic: it only needs the model's own denoised prediction, so it works
on any flow/eps model ComfyUI can sample. Calibrate with the same model patches
(shift, LoRAs), CFG and resolution you will sample with — the profile depends on
all of them. A latent batch > 1 averages the profile over several seeds.
"""

from __future__ import annotations

import logging

import torch
from comfy_api.latest import io

import comfy.sample
import comfy.samplers
import latent_preview

_EPS = 1e-8


def _equalize_sigmas(ref_sigmas: torch.Tensor, deltas: torch.Tensor,
                     steps: int, equalize: float) -> torch.Tensor:
    """Redistribute ``steps`` steps over the reference schedule.

    ``ref_sigmas`` has K+1 entries (last 0); ``deltas`` has K entries, the x0
    change over each reference interval. Each interval gets weight
    ``delta ** equalize``; the new sigmas sit at equal shares of the cumulative
    weight, linearly interpolated inside the reference interval they land in.
    """
    ref = ref_sigmas.double().cpu()
    w = (deltas.double().cpu().clamp_min(0) + _EPS) ** equalize
    cum = torch.cat([torch.zeros(1, dtype=torch.float64), torch.cumsum(w, 0)])
    cum = cum / cum[-1]

    targets = torch.linspace(0.0, 1.0, steps + 1, dtype=torch.float64)
    idx = torch.searchsorted(cum, targets, right=True).clamp(1, len(ref) - 1) - 1
    frac = (targets - cum[idx]) / (cum[idx + 1] - cum[idx]).clamp_min(_EPS)
    out = ref[idx] + frac.clamp(0, 1) * (ref[idx + 1] - ref[idx])
    out[0], out[-1] = ref[0], 0.0
    return out.float()


def _expected_deltas(ref_sigmas: torch.Tensor, deltas: torch.Tensor,
                     sigmas: torch.Tensor) -> torch.Tensor:
    """x0 change each step of ``sigmas`` should see, read off the reference profile.

    Assumes the change is spread uniformly in sigma inside each reference
    interval. Used for the console report and the self-check.
    """
    ref = ref_sigmas.double().cpu().flip(0)  # ascending for searchsorted
    cum = torch.cat([torch.zeros(1, dtype=torch.float64), torch.cumsum(deltas.double().cpu().flip(0), 0)])
    s = sigmas.double().cpu().clamp(float(ref[0]), float(ref[-1]))
    i = torch.searchsorted(ref, s, right=True).clamp(1, len(ref) - 1) - 1
    t = (s - ref[i]) / (ref[i + 1] - ref[i]).clamp_min(_EPS)
    c = cum[i] + t * (cum[i + 1] - cum[i])  # x0 change still to come below s
    return (c[:-1] - c[1:]).float()


def _rms_change(a: torch.Tensor, b: torch.Tensor) -> float:
    """RMS of the x0 change, per sample, averaged over the batch (= seeds)."""
    d = (a.float() - b.float()).flatten(1)
    return float(d.pow(2).mean(1).sqrt().mean())


# What Calibrate measured, handed to Equalize: the reference schedule and the x0
# change over each of its intervals. Splitting the nodes keeps the expensive pass
# cached while steps / equalize are tuned.
SigmaProfile = io.Custom("NKD_SIGMA_PROFILE")


class NKDSigmasCalibrate(io.ComfyNode):
    """Measure how much the model's prediction moves at every noise level."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="NKDSigmasCalibrate",
            display_name="😺NKD Sigmas Calibrate",
            category="😺NKD Nodes/Sampling",
            description=(
                "Runs one long reference pass and measures how much the predicted image "
                "changes at every noise level. Feed the profile to Sigmas Equalize; this "
                "node stays cached while you tune that one."
            ),
            inputs=[
                io.Model.Input("model"),
                io.Conditioning.Input("positive"),
                io.Conditioning.Input("negative"),
                io.Latent.Input("latent_image", tooltip="Same size you will sample at. A batch > 1 averages several seeds."),
                # No control_after_generate: a seed that changes every run would
                # throw the cached calibration away each time.
                io.Int.Input("seed", default=0, min=0, max=0xffffffffffffffff),
                io.Float.Input("cfg", default=3.0, min=0.0, max=100.0, step=0.1, round=False),
                io.Combo.Input("scheduler", options=comfy.samplers.SCHEDULER_NAMES, default="simple",
                               tooltip="Reference schedule for the calibration pass; also the result at equalize 0"),
                io.Int.Input("calib_steps", default=50, min=4, max=500,
                             tooltip="Steps of the reference pass. More = finer profile, slower"),
                io.Vae.Input("vae", optional=True,
                             tooltip="Connect to get the reference pass decoded on the image output"),
            ],
            outputs=[
                SigmaProfile.Output(display_name="profile"),
                io.Latent.Output(display_name="latent", tooltip="Result of the reference pass"),
                io.Image.Output(display_name="image", tooltip="Reference pass decoded (needs vae; black without it)"),
            ],
        )

    @classmethod
    def execute(cls, model, positive, negative, latent_image, seed: int, cfg: float,
                scheduler: str, calib_steps: int, vae=None) -> io.NodeOutput:
        ref_sigmas = comfy.samplers.calculate_sigmas(
            model.get_model_object("model_sampling"), scheduler, calib_steps).cpu()

        latent = comfy.sample.fix_empty_latent_channels(
            model, latent_image["samples"], latent_image.get("downscale_ratio_spacial", None),
            latent_image.get("downscale_ratio_temporal", None))
        noise = comfy.sample.prepare_noise(latent, seed, latent_image.get("batch_index", None))

        deltas: list[float] = []
        prev: list[torch.Tensor] = []
        preview = latent_preview.prepare_callback(model, len(ref_sigmas) - 1)

        def callback(step, x0, x, total_steps):
            cur = x0.tensors[0] if getattr(x0, "is_nested", False) else x0
            cur = cur.detach().to("cpu", torch.float32)
            if prev:
                deltas.append(_rms_change(cur, prev[0]))
            prev[:] = [cur]
            preview(step, x0, x, total_steps)

        samples = comfy.sample.sample_custom(
            model, noise, cfg, comfy.samplers.sampler_object("euler"), ref_sigmas,
            positive, negative, latent, noise_mask=latent_image.get("noise_mask"),
            callback=callback, disable_pbar=True, seed=seed)

        # Same shape KSampler hands out, so the reference can be decoded or
        # compared like any other sampler result.
        out_latent = latent_image.copy()
        out_latent.pop("downscale_ratio_spacial", None)
        out_latent.pop("downscale_ratio_temporal", None)
        out_latent["samples"] = samples

        if vae is not None:
            image = vae.decode(samples.unbind()[0] if samples.is_nested else samples)
            if image.ndim == 5:  # video VAEs: fold frames into the batch, as VAEDecode does
                image = image.reshape(-1, *image.shape[-3:])
        else:
            # ponytail: placeholder so a wired image output never breaks the graph
            image = torch.zeros(1, 64, 64, 3)

        # x0 is predicted at every sigma but the last (0), so the final interval
        # has no measurement: carry the last one over.
        while len(deltas) < len(ref_sigmas) - 1:
            deltas.append(deltas[-1] if deltas else 1.0)
        d = torch.tensor(deltas[: len(ref_sigmas) - 1])

        logging.info("[NKD Sigmas Calibrate] %s x%d: x0 change per step %s",
                     scheduler, calib_steps, ", ".join(f"{v:.4f}" for v in d.tolist()))
        return io.NodeOutput({"sigmas": ref_sigmas, "deltas": d, "scheduler": scheduler},
                             out_latent, image)


class NKDSigmasEqualize(io.ComfyNode):
    """Turn a calibration profile into a schedule; cheap, so tune it freely."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="NKDSigmasEqualize",
            display_name="😺NKD Sigmas Equalize",
            category="😺NKD Nodes/Sampling",
            description=(
                "Spreads the steps over a Sigmas Calibrate profile so each one does a "
                "comparable share of the work. Wire its SIGMAS into a custom sampler, "
                "or into Sigmas Curve's reference_sigmas to see it and redraw it."
            ),
            inputs=[
                SigmaProfile.Input("profile"),
                io.Int.Input("steps", default=20, min=1, max=10000, tooltip="Steps of the output schedule"),
                io.Float.Input("equalize", default=0.5, min=0.0, max=1.0, step=0.05,
                               tooltip="0 = reference schedule as is · 1 = every step changes the image equally"),
            ],
            outputs=[
                io.Sigmas.Output(),
                io.Float.Output(display_name="floats"),
            ],
        )

    @classmethod
    def execute(cls, profile: dict, steps: int, equalize: float) -> io.NodeOutput:
        ref_sigmas, d = profile["sigmas"], profile["deltas"]
        sigmas = _equalize_sigmas(ref_sigmas, d, steps, equalize)

        before = _expected_deltas(ref_sigmas, d, _equalize_sigmas(ref_sigmas, d, steps, 0.0))
        after = _expected_deltas(ref_sigmas, d, sigmas)
        spread = lambda v: float(v.max() / v.clamp_min(_EPS).min())  # noqa: E731
        logging.info("[NKD Sigmas Equalize] %s, %d steps, equalize %.2f: "
                     "per-step change max/min %.1fx -> %.1fx",
                     profile.get("scheduler", "?"), steps, equalize, spread(before), spread(after))
        logging.info("[NKD Sigmas Equalize] sigmas: %s",
                     ", ".join(f"{s:.4f}" for s in sigmas.tolist()))

        return io.NodeOutput(sigmas, sigmas.tolist())
