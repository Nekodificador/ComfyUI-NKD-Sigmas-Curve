"""
NKD H3 Audio Shift Curve — animate MiniMax H3's audio shift over the sampling run.

The stock MiniMaxH3SigmaShift node sets ``shift_audio`` once and it stays put for
the whole run. This node lets you draw how it changes instead: X is the sampling
progress, Y is a multiplier on that value.

It is a *multiplier*, not a replacement — MiniMaxH3SigmaShift still sets the level
and this only shapes it over time, so both nodes work together. A flat curve at
1.0x does nothing at all, which makes it safe to leave in a workflow.

Keep the multiplier range modest: the far ends of the dial trade coherence for
effect, so treat it as a nudge around the level you already like, not a way to
travel an order of magnitude away from it.

MiniMax H3 only; other models ignore it.
"""

import json
import logging

import torch
from comfy_api.latest import io

import comfy.patcher_extension

from .nkd_sigma_curve import _sample_curve

# Nominal audio shift for MiniMax H3 (comfy/ldm/minimax/model.py). The default
# curve sits flat on it, so dropping the node into a graph changes nothing until
# the curve is actually moved.
H3_NOMINAL_AUDIO_SHIFT = 3.0

_TO_KEY = "minimax_h3_sigma_shift_audio"

# The curve is a multiplier on the shift already in play, not a replacement for
# it. Absolute values would silently override whatever MiniMaxH3SigmaShift set,
# leaving that node's shift_audio looking dead — so the level stays its job and
# this node only shapes it over time.
_DEFAULT_MIN = 0.5
_DEFAULT_MAX = 2.0

# Never hand the model something outside this, whatever base * multiplier lands on.
_SHIFT_LIMITS = (0.5, 30.0)

_UNITY = (1.0 - _DEFAULT_MIN) / (_DEFAULT_MAX - _DEFAULT_MIN)

_DEFAULT_CURVE = json.dumps({
    "points":        [[0.0, _UNITY, 1.0], [1.0, _UNITY, 1.0]],
    "interpolation": "linear",
    "tension":       1.0,
})


def _parse_curve(curve_data: str) -> tuple[list[list[float]], str]:
    """Same JSON contract as the sigma editor: points, interpolation, tension."""
    try:
        data = json.loads(curve_data)
        raw = data.get("points", [])
        interpolation = str(data.get("interpolation", "smooth"))
    except (json.JSONDecodeError, ValueError, AttributeError):
        raw, interpolation = [], "smooth"

    if interpolation == "bspline":
        interpolation = "smooth"

    points: list[list[float]] = []
    for p in raw:
        try:
            px = max(0.0, min(1.0, float(p[0])))
            py = max(0.0, min(1.0, float(p[1])))
            pw = max(1.0, min(10.0, float(p[2]))) if len(p) > 2 else 1.0
            points.append([px, py, pw])
        except (IndexError, TypeError, ValueError):
            continue

    if len(points) < 2:
        points = [[0.0, _UNITY, 1.0], [1.0, _UNITY, 1.0]]

    points.sort(key=lambda p: p[0])
    return points, interpolation


def _progress(timestep: torch.Tensor, sample_sigmas) -> float:
    """Where in the schedule this call sits, as 0..1.

    Matched against the sampler's own sigma list rather than derived from the
    sigma value, so the curve's X axis is the step index the user sees in the
    editor even on a schedule that is nowhere near linear.
    """
    try:
        sigma = float(timestep.flatten()[0])
    except (AttributeError, IndexError, TypeError):
        return 0.0

    if sample_sigmas is not None and len(sample_sigmas) > 1:
        sigs = torch.as_tensor(sample_sigmas, dtype=torch.float32).flatten().cpu()
        idx = int(torch.argmin((sigs - sigma).abs()))
        return idx / (len(sigs) - 1)

    # No schedule in hand (a sampler that never set sample_sigmas): fall back to
    # the flow-model convention that sigma runs 1 -> 0.
    return 1.0 - max(0.0, min(1.0, sigma))


class NKDH3AudioShiftCurve(io.ComfyNode):
    """Per-step audio sigma shift for MiniMax H3, drawn as a curve."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="NKDH3AudioShiftCurve",
            display_name="😺NKD H3 Audio Shift Curve",
            category="😺NKD Nodes/Sampling",
            description=(
                "MiniMax H3 only: rewrite the audio stream's sigma shift on every "
                "sampling step from a drawn curve. Low shift = audio resolves early "
                "and the video locks onto it; high shift = both streams move together. "
                "Left-click to add points · Shift+click to remove · Drag to move."
            ),
            inputs=[
                io.Model.Input("model"),
                io.String.Input(
                    "curve_data",
                    default=_DEFAULT_CURVE,
                    socketless=True,
                    tooltip="Serialised curve JSON (managed by the curve widget)",
                ),
                io.Float.Input(
                    "mult_min",
                    default=_DEFAULT_MIN,
                    min=0.05,
                    max=10.0,
                    step=0.05,
                    round=False,
                    tooltip="Curve bottom (y=0) multiplies the incoming shift_audio by this",
                ),
                io.Float.Input(
                    "mult_max",
                    default=_DEFAULT_MAX,
                    min=0.05,
                    max=10.0,
                    step=0.05,
                    round=False,
                    tooltip="Curve top (y=1) multiplies the incoming shift_audio by this",
                ),
                io.Boolean.Input(
                    "debug",
                    default=False,
                    tooltip="Log the shift applied at each step to the console",
                ),
            ],
            outputs=[io.Model.Output()],
        )

    @classmethod
    def execute(
        cls,
        model,
        curve_data: str,
        mult_min: float,
        mult_max: float,
        debug: bool = False,
    ) -> io.NodeOutput:
        points, interpolation = _parse_curve(curve_data)
        lo, hi = float(mult_min), float(mult_max)

        def wrapper(executor, x, timestep, model_options, seed):
            to = model_options.get("transformer_options", {})
            t = _progress(timestep, to.get("sample_sigmas"))
            y = _sample_curve(points, t, interpolation)
            # Whatever set the level upstream (MiniMaxH3SigmaShift, or the
            # model's own default) stays in charge of it; the curve only bends
            # that value over the run.
            base = float(to.get(_TO_KEY, H3_NOMINAL_AUDIO_SHIFT))
            mult = lo + y * (hi - lo)
            shift = max(_SHIFT_LIMITS[0], min(_SHIFT_LIMITS[1], base * mult))
            if debug:
                logging.info("[NKD H3 Audio Shift] t=%.3f  base=%.3f  x%.3f  -> %.3f",
                             t, base, mult, shift)
            # Copy rather than mutate: model_options is shared across the run and
            # merged into every conditioning batch, so writing in place would leak
            # this step's value into anything that reads it later.
            new_to = dict(to)
            new_to[_TO_KEY] = shift
            new_options = dict(model_options)
            new_options["transformer_options"] = new_to
            return executor(x, timestep, new_options, seed)

        m = model.clone()
        m.add_wrapper_with_key(
            comfy.patcher_extension.WrappersMP.PREDICT_NOISE,
            "nkd_h3_audio_shift_curve",
            wrapper,
        )
        return io.NodeOutput(m)


# Registered from __init__.py alongside the sigma editor — the package exposes a
# single set of mappings, and importing this module from nkd_sigma_curve would
# close an import cycle (this one reads _sample_curve from there).
