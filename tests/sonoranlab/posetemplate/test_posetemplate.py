"""DYL-127: control images are drawn from template joints for any cell; golden against the Godot bake at 136."""

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from sonoranlab.posetemplate import COLORS, control_png, joints_px, load_template, render_openpose

REPO = Path(__file__).resolve().parents[3]
POSES = REPO / "templates/poses/run"
TEMPLATE = load_template(POSES / "template.yaml")
DIRS = ["north", "north-east", "east", "south-east", "south"]


@pytest.mark.parametrize("direction", DIRS)
@pytest.mark.parametrize("frame", range(6))
def test_golden_matches_godot_bake_at_cell_136(direction, frame):
    """Not byte-identical: the template stores joints rounded to 0.01 px and Godot rasterises in float32, so a few
    edge pixels differ (measured: 9..32 of ~5-6.6k lit pixels, IoU >= 0.9958). Everything structural is exact."""
    ref = np.array(Image.open(POSES / direction / f"pose_{frame:03d}.png").convert("RGB"))
    out = np.array(render_openpose(TEMPLATE, direction, frame, 136, 544))
    assert ref.shape == out.shape == (544, 544, 3)
    lit_ref, lit_out = ref.any(2), out.any(2)
    assert (ref != out).any(2).sum() <= 40
    assert (lit_ref & lit_out).sum() / (lit_ref | lit_out).sum() > 0.995
    # Limb (0.6x) and joint colours present in the bake are exactly the ones drawn here.
    colours = lambda a: {tuple(c) for c in a[a.any(2)]}  # noqa: E731
    assert colours(ref) <= colours(out)
    extra = colours(out) - colours(ref)  # east/1 only: a 5 px sliver of 2 colours the bake overdrew
    assert sum(((out == np.array(c)).all(2)).sum() for c in extra) <= 5
    # Every joint centre carries the joint's colour, as in the bake.
    for k, p in enumerate(joints_px(TEMPLATE, direction, frame, 136, 544)):
        if p is not None:
            x, y = int(p[0]), int(p[1])
            assert tuple(out[y, x]) == tuple(ref[y, x])
            assert tuple(out[y, x]) in {COLORS[k]} | {tuple(int(v * 0.6) for v in c) for c in COLORS} | set(COLORS)


@pytest.mark.parametrize("cell", [136, 144, 176, 184])
def test_control_is_square_multiple_with_foot_row_and_deterministic(cell):
    data = control_png(POSES / "template.yaml", "east", 0, cell)
    assert data == control_png(POSES / "template.yaml", "east", 0, cell)
    path = Path(__file__).with_name(f"_c{cell}.png")
    try:
        path.write_bytes(data)
        with Image.open(path) as im:
            size = im.size
            arr = np.array(im.convert("RGB"))
    finally:
        path.unlink(missing_ok=True)
    assert size == (cell * 4, cell * 4)
    # README foot rule at the gen canvas: the clip's lowest opaque row is (cell - 4) * s + 3. Joints sit above it
    # (mannequin is wider than the bone), and by the same margin for every cell, so registration is cell-independent.
    lowest = np.flatnonzero(arr.any(2).any(1))[-1]
    assert lowest <= (cell - 4) * 4 + 3
    assert abs((cell - 4) * 4 + 3 - lowest - 24) <= 3  # 24 px at cell 136; disc radius is round(4*W/512), so 23..26


def test_scaling_is_proportional_across_cells():
    a = joints_px(TEMPLATE, "south", 2, 136, 544)
    b = joints_px(TEMPLATE, "south", 2, 144, 576)
    for p, q in zip(a, b, strict=True):
        assert (p is None) == (q is None)
        if p:
            assert abs((q[0] - 576 / 2) - (p[0] - 544 / 2)) < 1e-6  # same s = 4, so same offsets


def test_unknown_frame_refused():
    with pytest.raises(ValueError, match="no frame"):
        render_openpose(TEMPLATE, "east", 9, 136)
    with pytest.raises(ValueError, match="no frame"):
        render_openpose(TEMPLATE, "west", 0, 136)
