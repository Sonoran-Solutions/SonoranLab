import hashlib
import json
from pathlib import Path
import subprocess
import sys

from PIL import Image, ImageDraw
import pytest

from benchmark.tools import assemble_set


TOOL = Path(assemble_set.__file__)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_attempt(root, relative, exit_code=0, kind=None, color="red"):
    directory = root / relative
    raw = directory / "raw"
    raw.mkdir(parents=True)
    if kind == "clip":
        for i in range(33):
            image = Image.new("RGBA", (32, 32))
            x = 4 + (i % 6) * 3
            ImageDraw.Draw(image).rectangle((x, 8, x + 5, 24), fill=color)
            image.save(raw / f"rgba_{i:05d}.png")
    elif kind == "frames":
        for i in range(2):
            Image.new("RGB", (8, 10), color).save(raw / f"rgba_{i}_00001_.png")
    elif kind == "grid":
        Image.new("RGB", (24, 16), color).save(raw / "out_00001_.png")
    manifest = {"job": f"{root.name}/{relative}", "exit_code": exit_code,
                "run": {"wall_s": None if exit_code else 12.5},
                "raw": {p.name: digest(p) for p in sorted(raw.glob("*.png"))}}
    (directory / "manifest.json").write_text(json.dumps(manifest) + "\n")
    return manifest["job"]


def read_selection(out):
    selection = json.loads((out / "selection.json").read_text())
    assert selection["schema"] == "sonoranlab.selection/1"
    assert selection["assembly"]["tool_sha256"] == digest(TOOL)
    assert selection["raw_frames"] == {p.name: digest(p) for p in sorted((out / "raw").glob("*.png"))}
    return selection


def test_e_selects_successful_retry_and_records_provenance(tmp_path):
    root = tmp_path / "2026-10-18"
    out = tmp_path / "assembled"
    failed = make_attempt(root, "E/samurai/2/north", exit_code=1)
    selected = make_attempt(root, "E/samurai/2/north-r1", kind="clip")
    for direction in assemble_set.DIRECTIONS[1:]:
        make_attempt(root, f"E/samurai/2/{direction}", kind="clip")
    make_attempt(root, "E/samurai/2/cost-north", kind=None)
    make_attempt(root, "E/samurai/2/smoke-north", kind=None)

    assert assemble_set.main([str(root), "E", "samurai", "2", "--out", str(out),
                              "--cell", "4", "--frames", "3"]) == 0
    selection = read_selection(out)
    assert selection["set"] == "E/samurai/2"
    assert selection["variant"] == ""
    assert selection["selected"]["north"] == selected
    assert selection["missing_parts"] == []
    assert len(selection["attempts"]) == 6
    north = [attempt for attempt in selection["attempts"] if attempt["part"] == "north"]
    assert [attempt["job"] for attempt in north] == [failed, selected]
    assert [attempt["retry"] for attempt in north] == [0, 1]
    assert [attempt["selected"] for attempt in north] == [False, True]
    assert [attempt["exit_code"] for attempt in north] == [1, 0]
    assert [attempt["wall_s"] for attempt in north] == [None, 12.5]
    for attempt in selection["attempts"]:
        assert attempt["manifest_sha256"] == digest(root / attempt["dir"] / "manifest.json")
    assert set(selection["raw_frames"]) == {
        f"{direction}_{i}.png" for direction in assemble_set.DIRECTIONS for i in range(3)}
    assert selection["assembly"]["params"] == {
        "cell": 4, "frames": 3, "scale": 3, "resize": "lanczos", "loop": {"frames": 3, "size": 12}}
    for direction, loop in selection["loops"].items():
        assert loop == json.loads((out / "loops" / direction / "loop.json").read_text())
        assert loop["source_frames"] == 33
        assert loop["clip"] == str(root / selection["selected"][direction].split("/", 1)[1] / "raw")
        for i in range(3):
            frame = out / "raw" / f"{direction}_{i}.png"
            assert frame.read_bytes() == (out / "loops" / direction / frame.name).read_bytes()
            with Image.open(frame) as image:
                assert image.size == (12, 12)
                assert image.mode == "RGBA"


def test_a_failed_attempt_keeps_set_on_record(tmp_path):
    root = tmp_path / "2026-10-18"
    out = tmp_path / "assembled"
    failed = make_attempt(root, "A/oathguard/1", exit_code=1)
    result = subprocess.run([sys.executable, str(TOOL), str(root), "A", "oathguard", "1", "--out", str(out)],
                            capture_output=True, text=True)
    assert result.returncode == 3, result.stderr
    selection = read_selection(out)
    assert selection["selected"] == {}
    assert selection["missing_parts"] == ["grid"]
    assert selection["raw_frames"] == {}
    assert len(selection["attempts"]) == 1
    assert selection["attempts"][0]["job"] == failed
    assert selection["attempts"][0]["selected"] is False
    assert "loops" not in selection


def test_refuses_existing_out_dir(tmp_path):
    root = tmp_path / "2026-10-18"
    make_attempt(root, "A/oathguard/1", exit_code=1)
    out = tmp_path / "assembled"
    out.mkdir()
    sentinel = out / "selection.json"
    sentinel.write_text("existing data")
    with pytest.raises(FileExistsError):
        assemble_set.main([str(root), "A", "oathguard", "1", "--out", str(out)])
    assert sentinel.read_text() == "existing data"
    assert list(out.iterdir()) == [sentinel]


def test_c_resizes_selected_frames_and_reports_missing_parts(tmp_path):
    root = tmp_path / "2026-10-18"
    out = tmp_path / "assembled"
    make_attempt(root, "C/oathguard/2/north-r1024", kind="frames", color="red")
    selected = make_attempt(root, "C/oathguard/2/north-r1024-r2", kind="frames", color="blue")
    make_attempt(root, "C/oathguard/2/north-r1024-r3", exit_code=1)
    make_attempt(root, "C/oathguard/2/east-r1024", exit_code=1)
    for ignored in ("north", "m-skel-native", "smoke-north", "s-cn10-north", "north-r1024-r7"):
        make_attempt(root, f"C/oathguard/2/{ignored}")

    assert assemble_set.main([str(root), "C", "oathguard", "2", "--out", str(out),
                              "--cell", "5", "--frames", "2"]) == 0
    selection = read_selection(out)
    assert selection["selected"] == {"north": selected}
    assert len(selection["attempts"]) == 4
    assert selection["missing_parts"] == list(assemble_set.DIRECTIONS[1:])
    assert set(selection["raw_frames"]) == {"north_0.png", "north_1.png"}
    assert selection["assembly"]["params"] == {"cell": 5, "frames": 2, "scale": 3, "resize": "lanczos"}
    for filename in selection["raw_frames"]:
        with Image.open(out / "raw" / filename) as image:
            assert image.size == (15, 15)
            assert image.mode == "RGBA"
            assert image.getpixel((7, 7)) == (0, 0, 255, 255)


@pytest.mark.parametrize("variant", ["ck3000", "ck2750"])
def test_b_slices_only_requested_variant_and_highest_successful_retry(tmp_path, variant):
    root = tmp_path / "2026-10-18"
    out = tmp_path / "assembled"
    make_attempt(root, f"B/relay_sentinel/1/{variant}", kind="grid")
    selected = make_attempt(root, f"B/relay_sentinel/1/{variant}-r6", kind="grid")
    make_attempt(root, f"B/relay_sentinel/1/{variant}-r2", exit_code=1)
    other = "ck2750" if variant == "ck3000" else "ck3000"
    make_attempt(root, f"B/relay_sentinel/1/{other}")
    make_attempt(root, "B/relay_sentinel/1/smoke-other")
    args = [str(root), "B", "relay_sentinel", "1", "--out", str(out)]
    if variant != "ck3000":
        args += ["--variant", variant]

    assert assemble_set.main(args) == 0
    selection = read_selection(out)
    assert selection["set"] == f"B/relay_sentinel/1/{variant}"
    assert selection["variant"] == variant
    assert selection["selected"] == {"grid": selected}
    assert [attempt["retry"] for attempt in selection["attempts"]] == [0, 2, 6]
    assert [attempt["selected"] for attempt in selection["attempts"]] == [False, False, True]
    assert set(selection["raw_frames"]) == {
        f"{direction}_{i}.png" for direction in ("south", "west", "east", "north") for i in range(3)}
    assert selection["assembly"]["params"] == {
        "cell": 136, "frames": 3, "scale": 4, "resize": "none",
        "slice": {"cols": 4, "rows": 4, "walk_cols": 3, "bg_tol": 40}}
    with Image.open(out / "raw" / "north_0.png") as image:
        assert image.size == (6, 6)


def test_a_samurai_seed_one_uses_only_smoke_try2(tmp_path):
    root = tmp_path / "2026-10-18"
    out = tmp_path / "assembled"
    selected = make_attempt(root, "A/samurai/1/smoke-try2", kind="grid")
    make_attempt(root, "A/samurai/1/smoke")
    make_attempt(root, "A/samurai/1/other")
    make_attempt(root, "A/samurai/1-r6")

    assert assemble_set.main([str(root), "A", "samurai", "1", "--out", str(out)]) == 0
    selection = read_selection(out)
    assert selection["selected"] == {"grid": selected}
    assert len(selection["attempts"]) == 1
    assert len(selection["raw_frames"]) == 12
    assert selection["assembly"]["params"]["slice"]["cols"] == 6
    with Image.open(out / "raw" / "north_0.png") as image:
        assert image.size == (4, 4)


def test_failed_attempt_without_timing_is_recorded(tmp_path):
    """A refused or OOM-killed attempt's manifest can lack run.wall_s; it is kept with wall_s None."""
    results = tmp_path / "2026-10-18"
    bad = results / "A" / "oathguard" / "1"
    bad.mkdir(parents=True)
    (bad / "manifest.json").write_text(json.dumps({"job": "2026-10-18/A/oathguard/1", "exit_code": 6,
                                                   "run": {"status": "no-response"}, "raw": {}}))
    out = tmp_path / "set"
    assert assemble_set.main([str(results), "A", "oathguard", "1", "--out", str(out)]) == 3
    sel = json.loads((out / "selection.json").read_text())
    assert sel["attempts"][0]["wall_s"] is None and sel["selected"] == {}
