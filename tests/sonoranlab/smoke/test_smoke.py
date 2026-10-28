"""DYL-131 smoke render: layout, project generation, objective checks, supervision and refusals.
Real Godot is never started: the engine is a pure-Python fake that draws what the script would."""

import json
import os
import signal
import sys
import time
from pathlib import Path

import numpy as np
import pytest
import synth
from PIL import Image

from sonoranlab import profile as profiles
from sonoranlab import smoke
from sonoranlab.cli import main


def jobs(lab, siblings=("ally", "boss"), candidate="hero"):
    cand = synth.export_job(lab.work, candidate)
    sibs = {s: synth.export_job(lab.work, s) for s in siblings}
    return cand, sibs


def parts_for(lab, labels=("hero", "ally", "boss", "kin")):
    prof = profiles.load_profile(lab.profile)
    return [smoke.load_sheet(synth.export_job(lab.work, name), name, prof) for name in labels]


def run(lab, out="out", engine="ok", shader=None, siblings=("ally", "boss"), **kw):
    cand, sibs = jobs(lab, siblings)
    return smoke.run_smoke(profile=lab.profile, candidate=cand, siblings=sibs, out=lab.root / out, shader=shader, **kw)


# ---------------------------------------------------------------- layout / enumeration


def test_each_character_keeps_its_own_cell_bottom_aligned_and_uncropped(lab):
    parts = parts_for(lab)
    assert [p.cell for p in parts] == [24, 32, 40, 16]
    tallest = max(p.cell for p in parts)
    for spec in smoke.plan_renders(parts):
        boxes = []
        for q in spec.quads:
            c = parts[q.part].cell
            assert q.x >= 0 and q.x + c <= spec.width and q.y >= 0 and q.y + c <= spec.height  # nothing cropped
            # every cell bottom sits on a line baseline shared by all characters
            assert (q.y + c - tallest - smoke.MARGIN) % (tallest + smoke.GROUP_GAP) == 0
            boxes.append((q.x, q.y, q.x + c, q.y + c))
        for i, a in enumerate(boxes):  # no two cells overlap
            for b in boxes[i + 1 :]:
                assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]


def test_stills_and_every_clip_frame_enumerated_with_mirrored_rows(lab):
    parts = parts_for(lab)
    specs = smoke.plan_renders(parts)
    got = {s.name for s in specs}
    want = {f"{r}_{c}_{x}x" for r in synth.ORDER for c in ("still", "idle", "run") for x in (1, 4)}
    want |= {"overview_1x", "overview_4x"}
    assert got == want
    run_spec = next(s for s in specs if s.name == "west_run_4x")
    assert len(run_spec.quads) == 3 * 4  # 3 frames x 4 characters
    west = parts[0].rows.index("west")
    cols = sorted({q.col for q in run_spec.quads if q.part == 0})
    assert cols == [3, 4, 5] and {q.row for q in run_spec.quads} == {west}  # the sheet's own (mirrored) west row
    overview = next(s for s in specs if s.name == "overview_1x")
    assert {q.col for q in overview.quads} == {0} and {q.row for q in overview.quads} == set(range(8))
    assert next(s for s in specs if s.name == "north_idle_1x").meta["timing"]["seconds"] == 0.4


def test_sibling_with_other_frame_count_wraps_and_missing_clip_shows_still(lab):
    prof = profiles.load_profile(lab.profile)
    four = [{"name": "idle", "start": 1, "frames": 4, "fps": 5, "loop": True}]
    sib = smoke.load_sheet(synth.export_job(lab.work, "ally", four), "ally", prof)
    hero = smoke.load_sheet(synth.export_job(lab.work, "hero"), "hero", prof)
    specs = {s.name: s for s in smoke.plan_renders([hero, sib])}
    idle = specs["north_idle_1x"]
    assert [q.col for q in idle.quads if q.part == 1] == [1, 2, 3, 4]  # the longest clip sets the strip
    assert [q.col for q in idle.quads if q.part == 0] == [1, 2, 1, 2]  # the 2-frame candidate wraps
    run_ = specs["north_run_1x"]
    assert {q.col for q in run_.quads if q.part == 1} == {0}
    assert run_.meta["frames_per_character"] == {"hero": 3, "ally": None}


def test_mirrored_rows_check(lab):
    sheet = smoke.load_sheet(synth.export_job(lab.work, "hero"), "hero", profiles.load_profile(lab.profile))
    assert smoke.mirrored_rows_exact(sheet, synth.MIRROR)["pass"]
    c = sheet.cell
    sheet.pixels[2 * c + 5, c + 9] = (1, 2, 3, 255)  # west frame 0 no longer flips east
    assert not smoke.mirrored_rows_exact(sheet, synth.MIRROR)["pass"]


# ---------------------------------------------------------------- project files


def test_generated_project_text(lab, monkeypatch):
    shader = lab.root / "PixelStandee.gdshader"
    shader.write_text(synth.STAND_SHADER)
    calls = []
    synth.patch_engine(monkeypatch, calls=calls)
    doc = run(lab, shader=shader)
    out = lab.root / "out"
    plan = json.loads((out / "project/plan.json").read_text())
    assert plan["px_per_m"] == 62 and plan["shader"] == "res://PixelStandee.gdshader"
    assert [s["cell"] for s in plan["sheets"]] == [24, 32, 40] and all(s["columns"] == 6 for s in plan["sheets"])
    assert (out / "project/PixelStandee.gdshader").read_text() == synth.STAND_SHADER
    assert smoke.uniforms((out / "project/plain.gdshader").read_text()) >= set(smoke.REQUIRED_UNIFORMS)
    assert "default_texture_filter=0" in (out / "project/project.godot").read_text()
    assert "enable_file_logging=false" in (out / "project/project.godot").read_text()
    # the script sets exactly the uniforms PixelStandee.cs sets for a still / clip frame
    script = (out / "project/render_plan.gd").read_text()
    import re

    assert set(re.findall(r'set_shader_parameter\("(\w+)"', script)) == {"sheet", "grid", "cell", "cell_pixels"}
    assert doc["border"] == "rendered" and doc["settings"]["shader_sha256"]
    # the Godot invocation is isolated under OUT
    env = calls[-1][1]
    assert env["SLR_PRIV"].startswith(str(out)) and env["SLR_PROJ"] == str(out / "project")


def test_without_shader_border_is_not_rendered(lab, monkeypatch):
    synth.patch_engine(monkeypatch)
    doc = run(lab)
    assert doc["border"] == "not_rendered" and doc["settings"]["shader_sha256"] is None
    plan = json.loads((lab.root / "out/project/plan.json").read_text())
    assert plan["shader"] == "res://plain.gdshader"
    assert not (lab.root / "out/project/PixelStandee.gdshader").exists()


def test_shader_without_required_uniforms_refused_before_writing(lab):
    bad = lab.root / "bad.gdshader"
    bad.write_text("shader_type spatial;\nuniform vec2 grid;\n")
    with pytest.raises(smoke.SmokeError, match="lacks uniforms"):
        run(lab, shader=bad)
    assert not (lab.root / "out").exists()


# ---------------------------------------------------------------- end to end with the fake engine


@pytest.mark.parametrize("with_shader", [False, True])
def test_pass_with_fake_engine(lab, monkeypatch, with_shader):
    shader = None
    if with_shader:
        shader = lab.root / "PixelStandee.gdshader"
        shader.write_text(synth.STAND_SHADER)
    synth.patch_engine(monkeypatch)
    doc = run(lab, shader=shader)
    assert doc["exit"] == 0 and doc["status"] == "pass", doc["checks"]
    assert doc["engine"] == "ran" and doc["godot"] == "4.7.2.stable.fake"
    assert doc["agent_recommendation"] == "" and doc["owner_style_acceptance"] == ""
    on_disk = json.loads((lab.root / "out/smoke.json").read_text())
    assert on_disk["status"] == "pass"
    assert on_disk["inputs"]["candidate"]["png_sha256"] == doc["inputs"]["candidate"]["png_sha256"]
    assert len(on_disk["inputs"]["siblings"]) == 2
    expected = 2 + 8 * 3 * 2
    assert len(doc["renders"]) == expected
    for name, meta in doc["renders"].items():
        path = lab.root / "out/render" / f"{name}.png"
        assert smoke.sha256_file(path) == meta["sha256"]
        assert Image.open(path).size == tuple(meta["size"])
    assert not (lab.root / "out/project/raw").exists()
    c = doc["checks"]
    assert c["nearest_filtering"]["max_deviation_observed"] == 0
    assert c["all_stills_and_clip_frames_rendered"]["stills"] == 16
    if with_shader:
        assert c["alpha_and_border"]["border_colors_observed"] == {"keyline": [14, 11, 10], "outer": [255, 255, 255]}
    # background and a 4x block equal the source pixel
    img = np.asarray(Image.open(lab.root / "out/render/overview_4x.png"))
    assert tuple(img[0, 0]) == (0x3A, 0x3A, 0x3A)


@pytest.mark.parametrize("mode", ["blur", "semi"])
def test_filtering_or_alpha_defects_are_not_pass(lab, monkeypatch, mode):
    synth.patch_engine(monkeypatch, mode)
    doc = run(lab)
    assert doc["exit"] == 1 and doc["status"] == "fail"
    assert not doc["checks"]["nearest_filtering" if mode == "blur" else "alpha_and_border"]["pass"]


@pytest.mark.parametrize("mode", ["fail", "timeout"])
def test_engine_failure_is_not_pass(lab, monkeypatch, mode):
    synth.patch_engine(monkeypatch, mode)
    doc = run(lab)
    assert doc["exit"] == 1 and doc["status"] == "fail" and doc["engine"] == "failed"
    assert not doc["checks"]["render_complete"]["pass"]


def test_engine_unavailable_exits_3_never_pass(lab, monkeypatch, capsys):
    monkeypatch.setattr(smoke, "find_engine", lambda godot=None: (None, "godot not found"))
    cand, sibs = jobs(lab)
    argv = ["smoke-render", "--profile", str(lab.profile), "--candidate", str(cand), "--out", str(lab.root / "o")]
    argv += [a for k, v in sibs.items() for a in ("--sibling", f"{k}={v}")]
    assert main(argv) == 3
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "unavailable" and printed["engine"] == "unavailable"
    doc = json.loads((lab.root / "o/smoke.json").read_text())
    assert doc["engine"] == "unavailable" and doc["renders"] == {} and doc["engine_detail"] == "godot not found"
    assert doc["agent_recommendation"] == doc["owner_style_acceptance"] == ""


def test_find_engine_reports_missing_pieces(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GODOT", raising=False)
    assert smoke.find_engine(None)[0] is None


def test_game_sheet_sibling_with_characters_json(lab, monkeypatch):
    synth.patch_engine(monkeypatch)
    cand = synth.export_job(lab.work, "hero")
    staged = synth.game_sheet(lab.work, "kin")
    doc = smoke.run_smoke(
        profile=lab.profile, candidate=cand, siblings={"kin": staged}, out=lab.root / "g", shader=None
    )
    assert doc["status"] == "pass", doc["checks"]
    sib = doc["inputs"]["siblings"][0]
    assert sib["form"] == "game" and sib["cell"] == 16 and "characters.json" in sib["metadata_sha256"]
    assert next(iter(doc["renders"].values()))["scale"] in (1, 4)


# ---------------------------------------------------------------- source checks


def test_baked_border_and_semi_transparency_detected(lab, monkeypatch):
    synth.patch_engine(monkeypatch)
    sheet = synth.make_sheet("hero")
    c = 24
    cell = sheet[0:c, 0:c]
    ys, xs = np.nonzero(cell[..., 3])
    cell[ys.min() - 1 : ys.max() + 2, xs.min() - 1 : xs.max() + 2][..., :] = (255, 255, 255, 255)  # white box ring
    cell[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1] = (40, 60, 200, 255)
    sheet[0:c, 0:c] = cell
    sheet[c + 3, c + 3] = (9, 9, 9, 100)
    cand = synth.export_job(lab.work, "hero", sheet=sheet)
    doc = smoke.run_smoke(profile=lab.profile, candidate=cand, siblings={}, out=lab.root / "b")
    assert doc["status"] == "fail"
    assert not doc["checks"]["no_baked_border"]["pass"] and not doc["checks"]["alpha_binary"]["pass"]
    assert doc["checks"]["alpha_binary"]["per_sheet"]["hero"]["semi_transparent_pixels"] >= 1


def test_clip_timing_must_match_profile(lab, monkeypatch):
    synth.patch_engine(monkeypatch)
    clips = [{"name": "idle", "start": 1, "frames": 2, "fps": 9, "loop": True}]
    cand = synth.export_job(lab.work, "hero", clips)
    doc = smoke.run_smoke(profile=lab.profile, candidate=cand, siblings={}, out=lab.root / "t")
    assert doc["status"] == "fail"
    assert "idle" in doc["checks"]["frames_and_timing_match_manifest"]["mismatches"][0]
    assert doc["checks"]["frames_and_timing_match_manifest"]["profile_clips_absent_from_sheet"] == ["run"]


# ---------------------------------------------------------------- refusals


def test_sibling_outside_roots_and_symlinks_refused(lab, tmp_path_factory):
    cand = synth.export_job(lab.work, "hero")
    outside = synth.export_job(tmp_path_factory.mktemp("elsewhere"), "ally")
    with pytest.raises(smoke.SmokeError, match="outside"):
        smoke.run_smoke(profile=lab.profile, candidate=cand, siblings={"ally": outside}, out=lab.root / "x")
    inside = synth.export_job(lab.work, "ally")
    link = lab.work / "ally-link"
    link.symlink_to(outside)
    with pytest.raises(smoke.SmokeError):
        smoke.run_smoke(profile=lab.profile, candidate=cand, siblings={"ally": link}, out=lab.root / "x")
    png = inside / "export/ally/ally.png"
    real = png.with_name("real.png")
    png.rename(real)
    png.symlink_to(real)
    with pytest.raises(smoke.SmokeError, match="symlink"):
        smoke.run_smoke(profile=lab.profile, candidate=cand, siblings={"ally": inside}, out=lab.root / "x")
    assert not (lab.root / "x").exists()


def test_cli_refusals_exit_2(lab, capsys):
    cand, _sibs = jobs(lab)
    base = ["smoke-render", "--profile", str(lab.profile), "--candidate", str(cand)]
    assert main([*base, "--out", str(lab.root / "o"), "--sibling", "nopath"]) == 2
    four = [a for s in ("ally", "boss", "kin", "extra") for a in ("--sibling", f"{s}={synth.export_job(lab.work, s)}")]
    assert main([*base, "--out", str(lab.root / "o"), *four]) == 2
    assert "at most 3" in capsys.readouterr().err
    assert main([*base, "--out", str(smoke.REPO / "smoke-out")]) == 2
    assert not (smoke.REPO / "smoke-out").exists()
    (lab.root / "full").mkdir()
    (lab.root / "full/x").write_text("x")
    assert main([*base, "--out", str(lab.root / "full")]) == 2


def test_game_checkout_and_label_rules(lab):
    cand = synth.export_job(lab.work, "hero")
    with pytest.raises(smoke.SmokeError, match="outside the repository and the game"):
        smoke.run_smoke(profile=lab.profile, candidate=cand, siblings={}, out=lab.work / "game" / "o")
    with pytest.raises(smoke.SmokeError, match="sibling id"):
        smoke.run_smoke(profile=lab.profile, candidate=cand, siblings={"Bad-Id": cand}, out=lab.root / "x")
    with pytest.raises(smoke.SmokeError, match="is the candidate"):
        smoke.run_smoke(profile=lab.profile, candidate=cand, siblings={"hero": cand}, out=lab.root / "x")


def test_pixel_budget_overflow_refused_before_any_write(lab, monkeypatch):
    monkeypatch.setattr(smoke, "MAX_TOTAL_PIXELS", 1000)
    with pytest.raises(smoke.SmokeError, match="budget"):
        run(lab)
    assert not (lab.root / "out").exists()


def test_group_wider_than_edge_refused(lab, monkeypatch):
    monkeypatch.setattr(smoke, "MAX_EDGE", 400)
    with pytest.raises(smoke.SmokeError, match="width limit"):
        run(lab)


def test_byte_budget_overflow_is_not_pass(lab, monkeypatch):
    synth.patch_engine(monkeypatch)
    monkeypatch.setattr(smoke, "MAX_TOTAL_BYTES", 2000)
    doc = run(lab)
    assert doc["exit"] == 1 and "byte budget" in " ".join(doc["checks"]["render_complete"]["missing"])
    assert sum(p.stat().st_size for p in (lab.root / "out/render").glob("*.png")) <= 2000


def test_profile_without_density_or_character_row(lab):
    cand = synth.export_job(lab.work, "hero")
    bare = synth.toy_profile(lab.work / "game", density=None)
    with pytest.raises(smoke.SmokeError, match="density"):
        smoke.run_smoke(profile=bare, candidate=cand, siblings={}, out=lab.root / "x")


# ---------------------------------------------------------------- supervision (real processes, no Godot)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"


def test_supervise_timeout_kills_the_whole_group(tmp_path):
    pidfile = tmp_path / "pid"
    code = f"sleep 60 & echo $! > {pidfile}; wait"
    t0 = time.monotonic()
    res = smoke.supervise(["bash", "-c", code], cwd=tmp_path, env=os.environ, timeout=1, grace=1)
    assert res["timed_out"] and time.monotonic() - t0 < 10
    child = int(pidfile.read_text())
    time.sleep(0.2)
    assert not alive(child)


def test_supervise_bounds_output_and_kills_flood(tmp_path):
    res = smoke.supervise(
        [sys.executable, "-c", "import sys\nwhile True: sys.stdout.write('x'*4096)"],
        cwd=tmp_path, env=os.environ, timeout=30, head=100, tail=100, max_output=1 << 20,
    )  # fmt: skip
    assert res["output_flood"] and len(res["head"]) == 100 and len(res["tail"]) <= 100
    assert res["bytes_total"] < 64 << 20


def test_supervise_normal_exit_and_cancel(tmp_path):
    ok = smoke.supervise(["bash", "-c", "echo hi; exit 3"], cwd=tmp_path, env=os.environ, timeout=10)
    assert ok["returncode"] == 3 and ok["head"].strip() == "hi" and not ok["timed_out"]
    res = smoke.supervise(["sleep", "60"], cwd=tmp_path, env=os.environ, timeout=60, grace=1, cancelled=lambda: True)
    assert res["cancelled"] and res["returncode"] == -signal.SIGTERM


def test_render_script_and_shell_wrapper_are_pinned():
    sh = (smoke.TOOL / "render.sh").read_text()
    assert "ulimit -f" in sh and 'XDG_DATA_HOME="$SLR_PRIV/data"' in sh and "env -i" in sh
    assert os.access(smoke.TOOL / "render.sh", os.X_OK)
