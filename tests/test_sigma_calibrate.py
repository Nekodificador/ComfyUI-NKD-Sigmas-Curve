"""Self-check for the sigma calibration maths (no model needed).

Run from the package folder with ComfyUI's python:

    python tests/test_sigma_calibrate.py
"""

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    # custom_nodes/<pkg>/ -> the ComfyUI root, so `comfy` and `latent_preview` resolve
    sys.path.insert(0, os.path.dirname(os.path.dirname(ROOT)))
    spec = importlib.util.spec_from_file_location(
        "nkd_sigma_calibrate", os.path.join(ROOT, "nkd_sigma_calibrate.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def demo():
    import torch
    m = _load()

    ref = torch.linspace(1.0, 0.0, 51)  # 50-step reference
    # Typical profile: big moves early, a long quiet tail.
    d = torch.exp(-torch.arange(50, dtype=torch.float32) / 6.0) + 0.01

    # equalize 0 hands back the reference schedule untouched.
    assert torch.allclose(m._equalize_sigmas(ref, d, 50, 0.0), ref, atol=1e-6)
    # ... and resamples it at any other step count.
    assert torch.allclose(m._equalize_sigmas(ref, d, 10, 0.0), torch.linspace(1.0, 0.0, 11), atol=1e-6)

    for steps, eq in [(8, 0.5), (20, 1.0), (33, 0.3)]:
        s = m._equalize_sigmas(ref, d, steps, eq)
        assert len(s) == steps + 1
        assert s[0] == ref[0] and s[-1] == 0.0
        assert bool((s[1:] < s[:-1]).all()), "sigmas must strictly decrease"

    # equalize 1: every output step sees the same x0 change.
    full = m._expected_deltas(ref, d, m._equalize_sigmas(ref, d, 20, 1.0))
    assert float(full.max() / full.min()) < 1.01, full
    assert abs(float(full.sum()) - float(d.sum())) < 1e-4

    # Intermediate equalize sits strictly between the two ends.
    spread = lambda v: float(v.max() / v.min())  # noqa: E731
    base = spread(m._expected_deltas(ref, d, m._equalize_sigmas(ref, d, 20, 0.0)))
    half = spread(m._expected_deltas(ref, d, m._equalize_sigmas(ref, d, 20, 0.5)))
    assert 1.01 < half < base, (half, base)

    # Steps pile up where the image moves: more of them in the first half of sigma.
    s = m._equalize_sigmas(ref, d, 20, 1.0)
    assert int((s > 0.5).sum()) > 11

    # Batch RMS change averages per-sample RMS.
    a = torch.zeros(2, 4, 8, 8)
    b = torch.cat([torch.ones(1, 4, 8, 8), 3 * torch.ones(1, 4, 8, 8)])
    assert abs(m._rms_change(b, a) - 2.0) < 1e-6

    # Equalize node works off the profile alone, no model in sight.
    profile = {"sigmas": ref, "deltas": d, "scheduler": "simple"}
    out = m.NKDSigmasEqualize.execute(profile, 12, 0.5)
    assert torch.allclose(out.result[0], m._equalize_sigmas(ref, d, 12, 0.5))
    assert len(out.result[1]) == 13

    # The calibration seed must stay put, or the cached pass reruns every time.
    seed = next(i for i in m.NKDSigmasCalibrate.define_schema().inputs if i.id == "seed")
    assert not getattr(seed, "control_after_generate", None)

    # Full Calibrate run against a stand-in sampler: x0 drifts by a known amount
    # each step, so the profile, latent and image outputs can all be checked.
    calls = {}

    def fake_sample(model, noise, cfg, sampler, sigmas, pos, neg, latent, noise_mask=None,
                    callback=None, disable_pbar=False, seed=None):
        n = len(sigmas) - 1
        for i in range(n):
            callback(i, torch.full_like(latent, float(i * i)), latent, n)
        calls["sigmas"] = sigmas
        return torch.full_like(latent, 7.0)

    class FakeVAE:
        def decode(self, s):
            return torch.ones(s.shape[0], 1, 32, 32, 3)  # 5D, like a video VAE

    class FakeModel:
        def get_model_object(self, name):
            return None

    m.comfy.samplers.calculate_sigmas = lambda ms, sch, steps: torch.linspace(1.0, 0.0, steps + 1)
    m.comfy.sample.fix_empty_latent_channels = lambda model, lat, *a: lat
    m.comfy.sample.sample_custom = fake_sample
    m.latent_preview.prepare_callback = lambda model, steps: (lambda *a: None)

    lat = {"samples": torch.zeros(2, 4, 8, 8), "downscale_ratio_spacial": 8}
    for vae in (FakeVAE(), None):
        res = m.NKDSigmasCalibrate.execute(FakeModel(), None, None, lat, 0, 3.0, "simple", 5, vae).result
        prof, out_lat, img = res
        # x0 = i*i, so the change between steps is 1, 3, 5, 7 and the
        # unmeasured last interval repeats 7.
        assert prof["deltas"].tolist() == [1.0, 3.0, 5.0, 7.0, 7.0], prof["deltas"]
        assert torch.equal(prof["sigmas"], calls["sigmas"])
        assert torch.equal(out_lat["samples"], torch.full((2, 4, 8, 8), 7.0))
        assert "downscale_ratio_spacial" not in out_lat and "downscale_ratio_spacial" in lat
        assert img.ndim == 4 and img.shape[-1] == 3
        assert img.shape[0] == (2 if vae else 1)

    # Video: 5D latent [B, C, T, H, W] goes through unchanged.
    vid = {"samples": torch.zeros(1, 16, 5, 8, 8)}
    prof, out_lat, img = m.NKDSigmasCalibrate.execute(FakeModel(), None, None, vid, 0, 3.0, "simple", 5, FakeVAE()).result
    assert prof["deltas"].tolist() == [1.0, 3.0, 5.0, 7.0, 7.0]
    assert out_lat["samples"].shape == (1, 16, 5, 8, 8) and img.ndim == 4

    # Two experts (Wan 2.2): high model above boundary, low model below, the
    # second segment starts from the first one's result with no fresh noise.
    seen = []

    def fake_sample2(model, noise, cfg, sampler, sigmas, pos, neg, latent, noise_mask=None,
                     callback=None, disable_pbar=False, seed=None):
        seen.append((model, sigmas.tolist(), bool(noise.abs().sum() == 0), float(latent.flatten()[0])))
        n = len(sigmas) - 1
        base = 0 if model == "high" else 3  # global step index of this segment's first step
        for i in range(n):
            callback(i, torch.full_like(latent, float((base + i) ** 2)), latent, n)
        return torch.full_like(latent, 7.0 if model == "high" else 9.0)

    m.comfy.sample.sample_custom = fake_sample2
    m.comfy.sample.prepare_empty_noise = torch.zeros_like
    FakeModel.__eq__ = lambda self, o: o == "high"
    res = m.NKDSigmasCalibrate.execute(FakeModel(), None, None, lat, 0, 3.0, "simple", 5, None,
                                       model_low="low", boundary=0.5).result
    prof, out_lat, _ = res
    # sigmas 1, .8, .6, .4, .2, 0 -> 3 above 0.5: high runs 1..0.4, low 0.4..0
    assert seen[0][1] == _Approx([1.0, 0.8, 0.6, 0.4]) and not seen[0][2]
    assert seen[1][0] == "low" and seen[1][1] == _Approx([0.4, 0.2, 0.0])
    assert seen[1][2] and seen[1][3] == 7.0, seen[1]
    assert prof["deltas"].tolist() == [1.0, 3.0, 5.0, 7.0, 7.0], prof["deltas"]
    assert prof["boundary"] == 0.5
    assert torch.equal(out_lat["samples"], torch.full((2, 4, 8, 8), 9.0))

    # Equalize hands back where to split for the two samplers (SplitSigmas step).
    out = m.NKDSigmasEqualize.execute(prof, 10, 0.0).result
    k = out[2]
    assert out[0][k - 1] >= 0.5 > out[0][k], (k, out[0])
    assert m.NKDSigmasEqualize.execute({"sigmas": ref, "deltas": d}, 10, 0.0).result[2] == 0

    print("sigma calibrate OK")


class _Approx(list):
    def __eq__(self, other):
        return len(self) == len(other) and all(abs(a - b) < 1e-6 for a, b in zip(self, other))


if __name__ == "__main__":
    demo()
