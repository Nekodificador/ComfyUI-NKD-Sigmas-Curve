"""
NKD Sigma Curve – ComfyUI custom node for interactive sigma schedule editing.

Allows controlling diffusion model sigma values through a visual spline curve
editor. The curve maps normalised step positions [0, 1] to normalised sigma
values [0, 1], which are then scaled by max_sigma.

Interpolation modes:
  "smooth"  – NURBS cubic (order 4) with clamped knot vector and per-point
              rational weights. w=1 → standard B-Spline (approximating);
              w>1 → curve is pulled closer to the control point.
  "linear"  – Piecewise linear between control points.
"""

from __future__ import annotations

import json
import os
import re
import torch

from comfy_api.latest import ComfyExtension, io
from typing_extensions import override


# ─── NURBS mathematics ──────────────────────────────────────────────────────

_NURBS_DEGREE = 3
_NURBS_TABLE_SIZE = 500


def _nurbs_knot_vector(n_pts: int, degree: int) -> list[float]:
    """
    Build a clamped (open) uniform knot vector for ``n_pts`` control points
    and the given ``degree``.

    Returns a list of length ``n_pts + degree + 1`` with the first and last
    ``degree + 1`` values clamped to 0 and 1 respectively.
    """
    order = degree + 1
    n_knots = n_pts + order
    if n_pts <= degree:
        return [0.0] * n_knots

    knots: list[float] = []
    # Clamped start
    for _ in range(order):
        knots.append(0.0)
    # Interior knots (uniform)
    n_interior = n_pts - degree
    for i in range(1, n_interior):
        knots.append(i / n_interior)
    # Clamped end
    for _ in range(order):
        knots.append(1.0)
    return knots


def _nurbs_basis(knots: list[float], n_pts: int, degree: int, u: float) -> list[float]:
    """
    Evaluate all B-spline basis functions N_{i,degree}(u) using the iterative
    Cox–de Boor algorithm.

    Returns a list of ``n_pts`` basis values.
    """
    # Clamp u to valid range
    u = max(knots[degree], min(knots[n_pts], u))
    # Handle exact endpoint
    if u >= knots[n_pts]:
        u = knots[n_pts] - 1e-10

    # Degree 0
    N = [0.0] * (len(knots) - 1)
    for i in range(len(N)):
        if knots[i] <= u < knots[i + 1]:
            N[i] = 1.0

    # Build up to target degree
    for p in range(1, degree + 1):
        N_new = [0.0] * len(N)
        for i in range(len(N) - p):
            d1 = knots[i + p] - knots[i]
            d2 = knots[i + p + 1] - knots[i + 1]
            c1 = ((u - knots[i]) / d1 * N[i]) if d1 > 0 else 0.0
            c2 = ((knots[i + p + 1] - u) / d2 * N[i + 1]) if d2 > 0 else 0.0
            N_new[i] = c1 + c2
        N = N_new

    return N[:n_pts]


def _nurbs_evaluate(
    points: list[list[float]],
    weights: list[float],
    knots: list[float],
    degree: int,
    u: float,
) -> tuple[float, float]:
    """
    Evaluate the rational NURBS curve at parameter ``u``.

    Returns (x, y) on the curve.
    """
    n_pts = len(points)
    N = _nurbs_basis(knots, n_pts, degree, u)

    wx = 0.0
    wy = 0.0
    w_sum = 0.0
    for i in range(n_pts):
        nw = N[i] * weights[i]
        wx += nw * points[i][0]
        wy += nw * points[i][1]
        w_sum += nw

    if w_sum == 0.0:
        return (0.0, 0.0)
    return (wx / w_sum, wy / w_sum)


def _nurbs_build_table(
    points: list[list[float]],
    weights: list[float],
    knots: list[float],
    degree: int,
    n_samples: int = _NURBS_TABLE_SIZE,
) -> tuple[list[float], list[float]]:
    """
    Pre-sample the NURBS curve into a lookup table of (x, y) pairs.
    """
    xs: list[float] = []
    ys: list[float] = []
    for i in range(n_samples + 1):
        u = i / n_samples
        x, y = _nurbs_evaluate(points, weights, knots, degree, u)
        xs.append(x)
        ys.append(y)
    return xs, ys


def _table_lookup_y(xs: list[float], ys: list[float], target_x: float) -> float:
    """
    Given a pre-sampled table, find y at ``target_x`` via binary search and
    linear interpolation.
    """
    n = len(xs)
    if n == 0:
        return 0.0
    if target_x <= xs[0]:
        return ys[0]
    if target_x >= xs[-1]:
        return ys[-1]

    # Binary search
    lo, hi = 0, n - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if xs[mid] <= target_x:
            lo = mid
        else:
            hi = mid

    dx = xs[hi] - xs[lo]
    if dx == 0:
        return ys[lo]
    t = (target_x - xs[lo]) / dx
    return ys[lo] + t * (ys[hi] - ys[lo])


# ─── Universal curve sampler ──────────────────────────────────────────────────

def _sample_curve(
    points: list[list[float]],
    t: float,
    interpolation: str,
    tension: float = 0.0,
) -> float:
    """
    Sample the curve at normalised position ``t`` ∈ [0, 1].

    Args:
        points:        Sorted list of [x, y, w] or [x, y] pairs (x, y ∈ [0, 1]).
                       The optional third element ``w`` is the NURBS weight
                       (w=1 → standard B-Spline; w>1 → closer to the point).
        t:             Horizontal position to query.
        interpolation: ``"smooth"`` → NURBS cubic spline;
                       ``"linear"`` → piecewise linear.
        tension:       Unused (kept for signature compatibility).

    Returns:
        Interpolated Y value clamped to [0, 1].
    """
    n = len(points)
    if n == 0:
        return 0.0
    if n == 1:
        return float(points[0][1])

    t = max(0.0, min(1.0, t))

    if t <= points[0][0]:
        return float(points[0][1])
    if t >= points[-1][0]:
        return float(points[-1][1])

    # Linear mode
    if interpolation == "linear":
        seg = 0
        for i in range(n - 1):
            if points[i][0] <= t <= points[i + 1][0]:
                seg = i
                break
        x0, y0 = float(points[seg][0]), float(points[seg][1])
        x1, y1 = float(points[seg + 1][0]), float(points[seg + 1][1])
        if x1 == x0:
            return y0
        local_t = (t - x0) / (x1 - x0)
        return float(y0 + local_t * (y1 - y0))

    # NURBS smooth — use degree = min(3, n-1) so 3 points get quadratic, 4+ get cubic
    # Endpoints always w=1 so interior weights create a meaningful differential
    degree = min(_NURBS_DEGREE, n - 1)
    weights = [
        1.0 if (i == 0 or i == n - 1) else (float(p[2]) if len(p) > 2 else 1.0)
        for i, p in enumerate(points)
    ]
    knots = _nurbs_knot_vector(n, degree)
    xs, ys = _nurbs_build_table(points, weights, knots, degree)
    result = _table_lookup_y(xs, ys, t)
    return float(max(0.0, min(1.0, result)))


# ─── Node definition ──────────────────────────────────────────────────────────

_DEFAULT_CURVE = json.dumps({
    "points":        [[0.0, 1.0, 1.0], [1.0, 0.0, 1.0]],
    "interpolation": "smooth",
    "tension":       1.0,
})


class NKDSigmaCurve(io.ComfyNode):
    """
    Interactive sigma curve editor.

    Outputs a SIGMAS tensor whose values are sampled from the user-drawn curve.
    The X-axis is the normalised step position (0 = first step, 1 = last step)
    and the Y-axis is the normalised sigma magnitude (y=1 → max_sigma, y=0 → 0).

    Two interpolation modes are available:
      "smooth"  – Cardinal / Hermite spline with per-point tension weights
      "linear"  – Piecewise linear between control points

    The final sigma is always 0 as required by ComfyUI samplers.
    """

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="NKDSigmasCurve",
            display_name="😺NKD Sigmas Curve",
            category="😺NKD Nodes/Sampling",
            is_output_node=True,
            description=(
                "Control sigma values interactively with a spline curve. "
                "Left-click to add points · Shift+click to remove · Drag to move."
            ),
            inputs=[
                io.String.Input(
                    "curve_data",
                    default=_DEFAULT_CURVE,
                    socketless=True,
                    tooltip="Serialised curve JSON (managed by the curve widget)",
                ),
                io.Int.Input(
                    "steps",
                    default=20,
                    min=1,
                    max=10000,
                    tooltip="Number of sigma steps to generate",
                ),
                io.Float.Input(
                    "max_sigma",
                    default=1.0,
                    min=0.001,
                    max=1.0,
                    step=0.001,
                    round=False,
                    tooltip="Maximum sigma value — curve top (y=1) maps to this",
                ),
                io.Sigmas.Input(
                    "reference_sigmas",
                    optional=True,
                    tooltip="Optional reference sigmas to display as a ghost overlay in the curve editor",
                ),
            ],
            outputs=[
                io.Sigmas.Output(),
                io.Float.Output(display_name="floats"),
            ],
        )

    @classmethod
    def execute(
        cls,
        curve_data: str,
        steps: int,
        max_sigma: float,
        reference_sigmas: torch.Tensor | None = None,
    ) -> io.NodeOutput:
        """
        Sample the curve and return a SIGMAS tensor and a float list, both of
        length ``steps + 1``.

        Args:
            curve_data: JSON produced by the JS widget with keys:
                        ``points``, ``interpolation``, ``tension``.
            steps:      Number of diffusion steps.
            max_sigma:  Scale for the Y-axis (curve y=1 → this value).

        Returns:
            NodeOutput wrapping a FloatTensor of shape ``(steps + 1,)``.
        """
        # ── Parse and validate curve JSON ──────────────────────────────────
        try:
            data = json.loads(curve_data)
            raw_points    = data.get("points", [[0.0, 1.0, 1.0], [1.0, 0.0, 1.0]])
            interpolation = str(data.get("interpolation", "smooth"))
            tension       = float(data.get("tension", 1.0))
        except (json.JSONDecodeError, ValueError, AttributeError):
            raw_points    = [[0.0, 1.0, 1.0], [1.0, 0.0, 1.0]]
            interpolation = "smooth"
            tension       = 0.0

        tension = max(1.0, min(10.0, tension))

        # Remap legacy bspline mode
        if interpolation == "bspline":
            interpolation = "smooth"

        # Sanitise each point
        points: list[list[float]] = []
        for p in raw_points:
            try:
                px = max(0.0, min(1.0, float(p[0])))
                py = max(0.0, min(1.0, float(p[1])))
                pw = max(1.0, min(10.0, float(p[2]))) if len(p) > 2 else 1.0
                points.append([px, py, pw])
            except (IndexError, TypeError, ValueError):
                continue

        if len(points) < 2:
            points = [[0.0, 1.0, 1.0], [1.0, 0.0, 1.0]]

        # Sort by X so segments are well-defined
        points.sort(key=lambda p: p[0])

        # ── Sample the curve at steps+1 positions ──────────────────────────
        sigma_values: list[float] = []
        for i in range(steps + 1):
            t = i / steps if steps > 0 else 0.0
            y = _sample_curve(points, t, interpolation, tension)
            sigma_values.append(y * max_sigma)

        # Guarantee last sigma == 0.0 (ComfyUI sampler requirement)
        sigma_values[-1] = 0.0

        sigmas = torch.FloatTensor(sigma_values)

        # Pass reference sigmas back to the frontend as UI metadata so the
        # curve editor can draw the ghost overlay without polling the backend.
        ui_extra: dict = {}
        if reference_sigmas is not None:
            ui_extra["reference_sigmas"] = reference_sigmas.tolist()

        return io.NodeOutput(sigmas, sigma_values, ui=ui_extra)


# ─── Preset storage ─────────────────────────────────────────────────────────
#
# Curve presets are stored in the ComfyUI user directory so they survive
# custom-node updates and can be shared across sessions. Only user-created
# presets exist — for stock schedules (karras, simple, linear, …) users can
# already pick those directly in the KSampler.

_PRESET_NAME_RE = re.compile(r"^[\w \-().]{1,64}$")


def _user_presets_path() -> str:
    """Resolve the path to the user's preset JSON file in ComfyUI/user/."""
    try:
        import folder_paths  # type: ignore
        user_dir = folder_paths.get_user_directory()
    except Exception:
        # Fallback: alongside the custom node (worst case, never read-only)
        user_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)))
    os.makedirs(user_dir, exist_ok=True)
    return os.path.join(user_dir, "nkd_sigma_curve_presets.json")


def _read_user_presets() -> list[dict]:
    path = _user_presets_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [p for p in data if isinstance(p, dict) and "name" in p]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _write_user_presets(presets: list[dict]) -> None:
    path = _user_presets_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(presets, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _sanitise_preset(payload: dict) -> dict | None:
    """Validate and normalise an incoming preset payload from the frontend."""
    name = str(payload.get("name", "")).strip()
    if not _PRESET_NAME_RE.match(name):
        return None
    raw_points = payload.get("points")
    if not isinstance(raw_points, list) or len(raw_points) < 2:
        return None
    pts: list[list[float]] = []
    for p in raw_points:
        try:
            x = max(0.0, min(1.0, float(p[0])))
            y = max(0.0, min(1.0, float(p[1])))
            w = max(1.0, min(10.0, float(p[2]))) if len(p) > 2 else 1.0
            pts.append([x, y, w])
        except (IndexError, TypeError, ValueError):
            return None
    interp = payload.get("interpolation", "smooth")
    if interp not in ("smooth", "linear"):
        interp = "smooth"
    try:
        tension = max(1.0, min(10.0, float(payload.get("tension", 1.0))))
    except (TypeError, ValueError):
        tension = 1.0
    locked = bool(payload.get("endpointsLocked", True))
    return {
        "name": name,
        "points": pts,
        "interpolation": interp,
        "tension": tension,
        "endpointsLocked": locked,
    }


def _register_preset_routes() -> None:
    """Register the /nkd_sigma_curve/presets endpoints with ComfyUI's server."""
    try:
        from server import PromptServer  # type: ignore
        from aiohttp import web  # type: ignore
    except ImportError:
        return

    routes = PromptServer.instance.routes

    # GET and POST share one handler so the preset list survives hot reloads.
    @routes.route("*", "/nkd_sigma_curve/presets")
    async def presets(request):
        if request.method == "GET":
            return web.json_response({"user": _read_user_presets()})
        if request.method != "POST":
            raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        clean = _sanitise_preset(payload)
        if clean is None:
            return web.json_response({"error": "invalid preset"}, status=400)
        presets = _read_user_presets()
        # Replace if name exists (case-insensitive), else append
        idx = next(
            (i for i, p in enumerate(presets) if p.get("name", "").lower() == clean["name"].lower()),
            -1,
        )
        if idx >= 0:
            presets[idx] = clean
        else:
            presets.append(clean)
        _write_user_presets(presets)
        return web.json_response({"ok": True, "preset": clean})

    @routes.delete("/nkd_sigma_curve/presets/{name}")
    async def delete_preset(request):
        name = request.match_info.get("name", "").strip()
        if not name:
            return web.json_response({"error": "missing name"}, status=400)
        presets = _read_user_presets()
        new_presets = [p for p in presets if p.get("name", "").lower() != name.lower()]
        if len(new_presets) == len(presets):
            return web.json_response({"error": "not found"}, status=404)
        _write_user_presets(new_presets)
        return web.json_response({"ok": True})


_register_preset_routes()


# ─── V3 Extension entrypoint ──────────────────────────────────────────────────

class NKDSigmaCurveExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [NKDSigmaCurve]


async def comfy_entrypoint() -> NKDSigmaCurveExtension:
    return NKDSigmaCurveExtension()


# ─── Legacy mappings ──────────────────────────────────────────────────────────

NODE_CLASS_MAPPINGS = {
    "NKDSigmasCurve": NKDSigmaCurve,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "NKDSigmasCurve": "😺NKD Sigmas Curve",
}
