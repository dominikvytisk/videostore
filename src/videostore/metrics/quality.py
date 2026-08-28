"""Visual-quality metrics (spec section 24). These measure the COVER video's
fidelity, not payload recoverability — reported alongside benchmark results
so a profile's cost in visual quality is visible, but the optimization target
stays recoverable-payload-bits-per-second-of-video, not PSNR/SSIM/VMAF
themselves (spec section 51)."""
from __future__ import annotations

import json
import subprocess
import tempfile
from typing import Optional

import numpy as np

from videostore.video.io import FFMPEG


def psnr(a: np.ndarray, b: np.ndarray, max_val: float = 255.0) -> float:
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    mse = np.mean((a - b) ** 2)
    if mse == 0:
        return float("inf")
    return 10 * np.log10((max_val**2) / mse)


def ssim(a: np.ndarray, b: np.ndarray, window: int = 7, max_val: float = 255.0) -> float:
    """Windowed SSIM (Wang et al.) using a uniform (box) window via an
    integral-image trick, computed over the whole plane and averaged."""
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    c1 = (0.01 * max_val) ** 2
    c2 = (0.03 * max_val) ** 2

    def box_filter(x: np.ndarray) -> np.ndarray:
        cs = np.cumsum(np.cumsum(x, axis=0), axis=1)
        cs = np.pad(cs, ((1, 0), (1, 0)))
        h, w = x.shape
        wh, ww = window, window
        out_h, out_w = h - wh + 1, w - ww + 1
        s = (
            cs[wh:wh + out_h, ww:ww + out_w]
            - cs[0:out_h, ww:ww + out_w]
            - cs[wh:wh + out_h, 0:out_w]
            + cs[0:out_h, 0:out_w]
        )
        return s / (wh * ww)

    mu_a = box_filter(a)
    mu_b = box_filter(b)
    mu_a2, mu_b2, mu_ab = mu_a * mu_a, mu_b * mu_b, mu_a * mu_b
    sigma_a2 = box_filter(a * a) - mu_a2
    sigma_b2 = box_filter(b * b) - mu_b2
    sigma_ab = box_filter(a * b) - mu_ab

    num = (2 * mu_ab + c1) * (2 * sigma_ab + c2)
    den = (mu_a2 + mu_b2 + c1) * (sigma_a2 + sigma_b2 + c2)
    return float(np.mean(num / den))


def vmaf_score(distorted_path: str, reference_path: str) -> Optional[float]:
    """Runs ffmpeg's libvmaf filter. Returns None (not an error) if libvmaf
    isn't available in this ffmpeg build — VMAF is optional per spec section 24."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        log_path = f.name
    try:
        cmd = [
            FFMPEG,
            "-y",
            "-loglevel",
            "error",
            "-i",
            distorted_path,
            "-i",
            reference_path,
            "-lavfi",
            f"libvmaf=log_fmt=json:log_path={log_path}",
            "-f",
            "null",
            "-",
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            return None
        with open(log_path) as f:
            data = json.load(f)
        return float(data["pooled_metrics"]["vmaf"]["mean"])
    except Exception:
        return None
    finally:
        import os

        try:
            os.remove(log_path)
        except OSError:
            pass
