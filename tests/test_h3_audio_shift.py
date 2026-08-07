"""Self-check for the H3 audio shift curve.

Run from the ComfyUI root so `comfy` and `server` are importable:

    python custom_nodes/ComfyUI-NKD-Sigmas-Curve/tests/test_h3_audio_shift.py
"""

import importlib.util
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    """Import both modules without running the package __init__.

    nkd_sigma_curve registers its preset routes at import time, which needs a
    live PromptServer; stub it so the maths can be exercised headless.
    """
    # custom_nodes/<pkg>/ -> the ComfyUI root, so `comfy` and `server` resolve
    sys.path.insert(0, os.path.dirname(os.path.dirname(ROOT)))
    import server
    deco = lambda *a, **k: (lambda f: f)  # noqa: E731
    if getattr(server.PromptServer, "instance", None) is None:
        server.PromptServer.instance = types.SimpleNamespace(
            routes=types.SimpleNamespace(get=deco, post=deco, delete=deco, put=deco))

    pkg = types.ModuleType("nkdsc")
    pkg.__path__ = [ROOT]
    sys.modules["nkdsc"] = pkg

    def load(name):
        spec = importlib.util.spec_from_file_location(
            f"nkdsc.{name}", os.path.join(ROOT, f"{name}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"nkdsc.{name}"] = mod
        spec.loader.exec_module(mod)
        return mod

    load("nkd_sigma_curve")
    return load("nkd_h3_audio_shift")


def demo():
    import torch
    m = _load()
    lo, hi = m._DEFAULT_MIN, m._DEFAULT_MAX

    def mult_at(points, interp, t):
        return lo + m._sample_curve(points, t, interp) * (hi - lo)

    # Dropping the node in with an untouched curve must be a no-op: flat at 1x,
    # so whatever set the level upstream comes through unchanged.
    pts, interp = m._parse_curve(m._DEFAULT_CURVE)
    assert all(abs(mult_at(pts, interp, t / 10) - 1.0) < 1e-6 for t in range(11))

    # A broken payload falls back to that same unity, not to an edge.
    assert abs(m._parse_curve("{ broken")[0][0][1] - m._UNITY) < 1e-9

    # Curve bottom/top land exactly on the configured multiplier range.
    ramp = json.dumps({"points": [[0.0, 0.0, 1.0], [1.0, 1.0, 1.0]],
                       "interpolation": "linear"})
    p, i = m._parse_curve(ramp)
    assert abs(mult_at(p, i, 0.0) - lo) < 1e-6
    assert abs(mult_at(p, i, 0.5) - (lo + hi) / 2) < 1e-6
    assert abs(mult_at(p, i, 1.0) - hi) < 1e-6

    # Progress is the step index in the sampler's own schedule, not a function
    # of the sigma value — so an uneven schedule still maps 1:1 to the editor.
    sched = torch.tensor([1.0, 0.8, 0.55, 0.3, 0.0])
    assert [round(m._progress(torch.tensor([s]), sched), 3) for s in sched] == \
        [0.0, 0.25, 0.5, 0.75, 1.0]
    # Without a schedule in hand, fall back to the flow convention sigma: 1 -> 0.
    assert abs(m._progress(torch.tensor([0.25]), None) - 0.75) < 1e-6

    # The wrapper must write this step's shift without mutating the shared
    # model_options it was handed.
    seen = {}

    def executor(x, t, mo, seed):
        seen.clear()
        seen.update(mo["transformer_options"])
        return "out"

    class FakeModel:
        def clone(self):
            return self

        def add_wrapper_with_key(self, kind, key, fn):
            self.fn = fn

    def run(curve, base=None, lo_=lo, hi_=hi, sigma=0.55):
        to = {"sample_sigmas": sched, "untouched": 1}
        if base is not None:
            to["minimax_h3_sigma_shift_audio"] = base
        options = {"transformer_options": to}
        fake = FakeModel()
        m.NKDH3AudioShiftCurve.execute(fake, curve, lo_, hi_)
        assert fake.fn(executor, None, torch.tensor([sigma]), options, 0) == "out"
        # Never mutate the caller's dict — it is shared across the whole run.
        assert to.get("minimax_h3_sigma_shift_audio") == base
        assert seen["untouched"] == 1
        return seen["minimax_h3_sigma_shift_audio"]

    # Multiplies whatever MiniMaxH3SigmaShift left in place, rather than
    # replacing it — that node stays in charge of the level.
    assert abs(run(ramp, base=8.0) - 8.0 * (lo + hi) / 2) < 1e-6
    # With nothing upstream, the model's own nominal is the base.
    assert abs(run(ramp, base=None) - m.H3_NOMINAL_AUDIO_SHIFT * (lo + hi) / 2) < 1e-6
    # An untouched curve passes the upstream value through untouched.
    assert abs(run(m._DEFAULT_CURVE, base=8.0) - 8.0) < 1e-6
    # Absurd settings still land inside what the model can be handed.
    assert run(ramp, base=25.0, lo_=10.0, hi_=10.0) == m._SHIFT_LIMITS[1]

    print("h3 audio shift OK")


if __name__ == "__main__":
    demo()
