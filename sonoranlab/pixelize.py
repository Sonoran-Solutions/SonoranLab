"""CPU PixelOE processing for one sliced RGB frame, matching Phase 1's downscale.

PixelOE 1.0.0's torch/downscale/k_centroid.py::batched_kmeans initializes centroids
with linspace between patch minima/maxima. Its utils.py::batched_kmeans_iter uses
argmin and masked means, with no RNG; no seed or global RNG change is needed.
"""

from importlib.metadata import version

import numpy as np


def pixelize(rgb: np.ndarray, scale: int, *, method: str = "k_centroid", thickness: int = 0) -> np.ndarray:
    """Downscale one (H,W,3) uint8 frame without mutation; dimensions must divide by scale.

    Methods are k_centroid, contrast and nearest. Outline thickness is 0..6 HD
    pixels (the kernels supported by PixelOE 1.0.0). Slice sheets before calling.
    """
    if not isinstance(rgb, np.ndarray) or rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8:
        raise ValueError("rgb must be an (H,W,3) uint8 array")
    if isinstance(scale, bool) or not isinstance(scale, (int, np.integer)) or scale <= 0:
        raise ValueError("scale must be a positive integer")
    h, w, _ = rgb.shape
    if not h or not w or h % scale or w % scale:
        raise ValueError(f"frame {w}x{h} must have positive dimensions that are multiples of scale {scale}")
    if method not in ("k_centroid", "contrast", "nearest"):
        raise ValueError(f"unknown pixelize method {method!r}; choose k_centroid, contrast or nearest")
    if isinstance(thickness, bool) or not isinstance(thickness, (int, np.integer)) or not 0 <= thickness <= 6:
        raise ValueError("thickness must be an integer from 0 to 6 (PixelOE 1.0.0 kernels)")

    import torch
    from pixeloe.torch.pixelize import pixelize as pixeloe_pixelize

    t = torch.from_numpy(rgb.copy()).permute(2, 0, 1)[None].float() / 255
    with torch.no_grad():
        out = pixeloe_pixelize(
            t, pixel_size=scale, thickness=thickness, mode=method, no_post_upscale=True, backend="torch"
        )
    return np.rint(out[0].permute(1, 2, 0).clamp(0, 1).numpy() * 255).astype(np.uint8)


def versions() -> dict[str, str]:
    """Installed processing dependency versions for artifact manifests, without importing torch."""
    return {name: version(name) for name in ("pixeloe", "torch", "numpy", "pillow")}
