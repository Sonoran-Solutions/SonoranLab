"""`sonoranlab refprep`: synthetic HD turnaround -> eight candidate stills (pixel math only)."""

import json

import numpy as np
import pytest
from classreq_fixtures import CELL, ORDER, PALETTE, figure, hd_view, turnaround, write_request
from PIL import Image

from sonoranlab.cli import main


def refprep(tmp_path, profile, *extra, character="hero", name="out"):
    req = write_request(tmp_path / f"req-{character}.yaml", character=character)
    out = tmp_path / name
    argv = ["refprep", "--profile", str(profile), "--request", str(req), "--out", str(out), *map(str, extra)]
    return main(argv), out


def stills(out):
    return {d: np.asarray(Image.open(out / f"still_{d}.png")) for d in ORDER}


def test_turnaround_to_eight_stills(tmp_path, profile, capsys):
    img = turnaround(tmp_path / "turn.png")
    code, out = refprep(tmp_path, profile, "--turnaround", img, "--grid", "4x2")
    summary = json.loads(capsys.readouterr().out)
    assert code == 0 and summary["status"] == "ready" and summary["cell"] == CELL
    doc = json.loads((out / "refprep.json").read_text())
    assert doc["parameters"]["target_height_px"] == 43 and doc["parameters"]["common_scale"] == 0.25
    assert doc["check"]["status"] == "ready" and doc["check"]["findings"] == []
    pal = {tuple(c) for c in PALETTE}
    for d, f in stills(out).items():
        assert f.shape == (CELL, CELL, 4)
        opaque = f[..., 3] > 0
        assert set(np.unique(f[..., 3])) == {0, 255}
        assert np.nonzero(opaque)[0].max() == CELL - 4
        assert {tuple(c) for c in f[opaque, :3].tolist()} <= pal
        ys, xs = np.nonzero(opaque)
        crop = f[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
        assert np.array_equal(crop, figure(d, height=43)), d  # exact round trip of the drawn pixel figure
        assert doc["views"][d]["source"] == "turnaround"
    assert set(doc["outputs"]) == {f"still_{d}.png" for d in ORDER} | {f"crops/{d}_hd.png" for d in ORDER} | {
        "contact.png"
    }
    assert doc["inputs"]["turnaround"]["sha256"] and doc["inputs"]["profile"]["sha256"]
    # exclusive output: a second run refuses and leaves the first untouched
    before = (out / "refprep.json").read_bytes()
    code, _ = refprep(tmp_path, profile, "--turnaround", img)
    assert code == 2 and "exists" in capsys.readouterr().err
    assert (out / "refprep.json").read_bytes() == before


def test_deterministic_and_no_row_needs_profile_row(tmp_path, profile, capsys):
    img = turnaround(tmp_path / "turn.png")
    _, a = refprep(tmp_path, profile, "--turnaround", img, character="rogue", name="a")
    _, b = refprep(tmp_path, profile, "--turnaround", img, character="rogue", name="b")
    da, db = (json.loads((x / "refprep.json").read_text()) for x in (a, b))
    assert da["outputs"] == db["outputs"]
    assert da["check"]["status"] == "needs_profile_row" and da["parameters"]["cell_source"] == "computed"
    capsys.readouterr()


def test_blank_grid_cell_is_an_error_naming_the_direction(tmp_path, profile, capsys):
    img = turnaround(tmp_path / "turn.png", blank=("south-west",))
    code, out = refprep(tmp_path, profile, "--turnaround", img)
    assert code == 2 and "south-west" in capsys.readouterr().err and not out.exists()


def test_view_override_replaces_one_cell(tmp_path, profile, capsys):
    img = turnaround(tmp_path / "turn.png", blank=("south-west",))
    view = tmp_path / "sw.png"
    Image.fromarray(hd_view("south-west", size=300)).save(view)
    code, out = refprep(tmp_path, profile, "--turnaround", img, "--view", f"south-west={view}")
    assert code == 0, capsys.readouterr().err
    doc = json.loads((out / "refprep.json").read_text())
    assert doc["views"]["south-west"]["source"] == "view_override"
    assert doc["inputs"]["views"]["south-west"]["sha256"] == doc["views"]["south-west"]["sha256"]
    assert doc["views"]["west"]["source"] == "turnaround" and doc["check"]["status"] == "ready"
    capsys.readouterr()
    # all eight as views: no turnaround needed; seven views without a turnaround is a usage error
    views = []
    for d in ORDER:
        p = tmp_path / f"v-{d}.png"
        Image.fromarray(hd_view(d)).save(p)
        views += ["--view", f"{d}={p}"]
    assert refprep(tmp_path, profile, *views, name="all")[0] == 0
    assert refprep(tmp_path, profile, *views[:-2], name="seven")[0] == 2
    assert refprep(tmp_path, profile, "--turnaround", img, "--view", f"up={view}", name="bad")[0] == 2
    capsys.readouterr()


def test_noisy_key_and_residual_key_warning(tmp_path, profile, capsys):
    img = turnaround(tmp_path / "turn.png", noise=True)
    code, out = refprep(tmp_path, profile, "--turnaround", img)
    doc = json.loads((out / "refprep.json").read_text())
    assert code == 0 and doc["check"]["status"] == "ready"
    assert not [w for w in doc["warnings"] if w.startswith("residual_key")]
    # a near-key blob attached to one figure survives the matte and is reported
    arr = np.asarray(Image.open(img)).copy()
    arr[200:216, 100:140] = (200, 40, 200)
    Image.fromarray(arr).save(tmp_path / "blob.png")
    _, out2 = refprep(tmp_path, profile, "--turnaround", tmp_path / "blob.png", name="blob")
    doc2 = json.loads((out2 / "refprep.json").read_text())
    assert any(w.startswith("residual_key: north") for w in doc2["warnings"]), doc2["warnings"]
    assert doc2["views"]["north"]["residual_key_px"] > 0
    capsys.readouterr()


@pytest.mark.parametrize("args", [["--grid", "3x2"], ["--order", "north,south"], ["--cell", "100"], []])
def test_usage_errors(tmp_path, profile, capsys, args):
    img = turnaround(tmp_path / "turn.png")
    extra = args if args else []
    if args:
        extra = ["--turnaround", img, *args]
    code, out = refprep(tmp_path, profile, *extra)
    assert code == 2 and not out.exists()
