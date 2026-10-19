"""sonoranlab.validate + `sonoranlab validate` on tiny synthetic fixtures (tests/fixtures/dyl83, no game pixels).

Each defect fires its rule on exactly the intended frame and nothing else fires; anchor_mode foot keeps parity with
the calibrated Phase 1 validator (benchmark/tools/process.py). The real-data regression reads the 12 Phase 1 Track E
sets from SONORANLAB_WORK (default ~/sonoranlab-work) and skips when they are absent.
"""

import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml
from PIL import Image

from sonoranlab.cli import main
from sonoranlab.profile import load_palette, load_profile, sha256_file
from sonoranlab.validate import foot_slide, opening, read_frames, validate

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "tests" / "fixtures" / "dyl83"
sys.path.insert(0, str(FIX))
sys.path.insert(0, str(REPO / "benchmark" / "tools"))
import make_fixtures as M  # noqa: E402
import process as P  # noqa: E402

WORK = Path(os.environ.get("SONORANLAB_WORK", "~/sonoranlab-work")).expanduser()
E_SETS = WORK / "benchmark" / "results" / "2026-10-18" / "E"
GT = WORK / "benchmark" / "characters" / "2026-10-18"
LIMITS = SimpleNamespace(
    max_foot_slide_px=3, max_bbox_drift_px=1, opaque_area_cv_warn=0.10, loop_closure_ratio_warn=1.29
)
HOVER = SimpleNamespace(**vars(LIMITS), hover_x_drift_warn_px=3, hover_min_bob_px=1.5)
ACCESSORY = {"colours": [list(M.GOLD)], "min_pixels": 2, "min_share": 0.9}
DIRS = ["south", "east"]
KW = {
    "cell": (M.CELL, M.CELL),
    "frames": M.FRAMES,
    "directions": DIRS,
    "palette": M.PALETTE,
    "foot_y": M.FOOT_Y,
    "anchor_x": M.ANCHOR_X,
    "padding": M.PADDING,
    "limits": LIMITS,
    "max_colours": M.MAX_COLOURS,
    "loop": True,
    "accessory": ACCESSORY,
}
ROW_RE = re.compile(r"^(\w+): frame \d+ \(([a-z-]+)/(\d+)\)")
DIR_RE = re.compile(r"^(\w+): ([a-z-]+) ")
PROCESS_RULES = {"frame_count", "canvas", "empty", "palette", "semi_alpha", "colours", "touches_edge", "foot_slide"}
PROCESS_RULES |= {"bbox_drift", "foot_line", "registration"}  # the rules process.py (validate + run) has


def load(case: str) -> dict[str, list[np.ndarray]]:
    """`clean` with the case's frames from disk swapped in."""
    clips = {d: [np.asarray(Image.open(FIX / "clean" / f"{d}_{i}.png")) for i in range(M.FRAMES)] for d in DIRS}
    for p in sorted((FIX / case).glob("*.png")) if case != "clean" else []:
        d, i = p.stem.rsplit("_", 1)
        clips[d][int(i)] = np.asarray(Image.open(p))
    return clips


def keys(msgs: list[str]) -> set[tuple]:
    """(rule, direction, frame) of each message; frame None for direction-level, direction None for clip-level."""
    out = set()
    for m in msgs:
        if r := ROW_RE.match(m):
            out.add((r[1], r[2], int(r[3])))
        elif m.startswith("frame_count: expected"):  # process.py lists every wrong direction in one message
            out |= {("frame_count", d, None) for d in re.findall(r"'([a-z-]+)'", m.split("wrong/missing")[1])}
        elif m.startswith("registration: ") and "clipped" in m:  # process.run: one message per direction
            d, px = re.match(r"registration: ([a-z-]+) .* clipped \[([\d, ]+)\]", m).groups()
            out |= {("registration", d, i) for i, n in enumerate(map(int, px.split(","))) if n}
        elif (r := DIR_RE.match(m)) and r[2] in P.ROWS:
            out.add((r[1], r[2], None))
        else:
            out.add((m.split(":")[0], None, None))
    return out


def warn_keys(rep: dict) -> set[tuple]:
    return {k[:2] for k in keys(rep["warnings"])}


# case -> (overrides of KW / structural edits, failing (rule, direction, frame), warning (rule, direction))
def _drop(c):
    c["east"].pop()


def _mirror(c):
    c["west"] = [f[:, ::-1].copy() for f in c["east"]]
    c["west"][1][13, 12] = (*M.SKIN, 255)


CASES = {
    "clean": ({}, set(), set()),
    "shifted": ({}, {("bbox_drift", "east", 2), ("bbox_drift", "east", 3)}, set()),
    "foot_slide": ({}, {("foot_slide", "east", 1)}, set()),
    "off_palette": ({}, {("palette", "east", 1)}, set()),
    "fringe": ({}, {("palette", "east", 2), ("fringe", "east", 2)}, set()),  # a border pixel off the palette is both
    "semi_alpha": ({}, {("semi_alpha", "east", 1)}, set()),
    "touches_edge": ({}, {("touches_edge", "east", 2)}, set()),
    "canvas": ({}, {("canvas", "east", 1)}, set()),
    "colours": ({}, {("colours", "east", 2)}, set()),
    "foot_line": ({}, {("foot_line", "south", None)}, set()),
    "accessory": ({}, set(), {("accessory_presence", None)}),
    "blade": ({}, {("bbox_drift", "east", 2), ("bbox_drift", "east", 0)}, {("loop_closure_ratio", "east")}),
    "missing": ({"edit": _drop}, {("frame_count", "east", None)}, set()),
    "clipped": (
        {"registration": {"east": {"shift_px": [0, 0], "clipped_px": [0, 0, 7, 0]}}},
        {("registration", "east", 2)},
        set(),
    ),
    "mirror": ({"edit": _mirror, "mirror": {"west": "east"}}, {("direction_mirror", "west", 1)}, set()),
}


def run_case(case: str) -> tuple[dict, dict, dict]:
    extra, _, _ = CASES[case]
    extra = dict(extra)
    clips = load(case)
    if edit := extra.pop("edit", None):
        edit(clips)
    return validate(clips, **(KW | extra)), clips, extra


def no_numpy(x) -> bool:
    if isinstance(x, dict):
        return all(isinstance(k, str) and no_numpy(v) for k, v in x.items())
    if isinstance(x, list):
        return all(no_numpy(v) for v in x)
    return x is None or type(x) in (bool, int, float, str)


def test_fixtures_on_disk_match_the_generator():
    for case, frames in M.build().items():
        assert sorted(p.stem for p in (FIX / case).glob("*.png")) == sorted(frames), case
        for stem, f in frames.items():
            assert np.array_equal(np.asarray(Image.open(FIX / case / f"{stem}.png")), f), (case, stem)


@pytest.mark.parametrize("case", list(CASES))
def test_each_rule_fires_on_exactly_its_frame(case):
    rep, _, _ = run_case(case)
    _, fails, warns = CASES[case]
    assert keys(rep["failures"]) == fails
    assert warn_keys(rep) == warns
    assert rep["pass"] is (not rep["failures"]) is (not fails)
    bad = {(r["direction"], r["frame"]) for r in rep["frames"] if not r["ok"]}
    assert bad == {(d, i) for _, d, i in fails if i is not None}
    assert no_numpy(rep) and json.loads(json.dumps(rep)) == rep
    assert list(rep)[:3] == ["pass", "scope", "anchor_mode"] and "human review" in rep["scope"]


@pytest.mark.parametrize("case", [c for c in CASES if c != "canvas"])  # process.py cannot compare unequal canvases
def test_parity_with_process_validate(case):
    rep, clips, extra = run_case(case)
    ref = P.validate(
        clips, M.CELL, M.FRAMES, DIRS, M.PALETTE, loop=True, max_foot_slide=3, max_bbox_drift=1, cv_warn=0.10,
        loop_ratio_warn=1.29, max_colours=M.MAX_COLOURS,
    )  # fmt: skip
    for d, info in (extra.get("registration") or {}).items():  # what process.run adds after validate
        if any(info["clipped_px"]):
            ref["failures"].append(f"registration: {d} shift {info['shift_px']} clipped {info['clipped_px']} px")
    mine = {k for k in keys(rep["failures"]) if k[0] in PROCESS_RULES}
    assert mine == keys(ref["failures"])
    if mine == keys(rep["failures"]):
        assert rep["pass"] == (not ref["failures"])
    by_key = {(r["direction"], r["frame"]): r for r in ref["frames"]}
    for r in rep["frames"]:
        theirs = by_key[(r["direction"], r["frame"])]
        for k in ("bbox", "foot_y", "contact", "foot_x", "foot_slide", "bbox_drift", "opaque_pixels", "colours"):
            assert r[k] == theirs[k], (r["direction"], r["frame"], k)
    assert rep["cycle"] == ref["cycle"]
    for d, cyc in ref["directions"].items():
        assert {k: rep["directions"][d][k] for k in cyc} == cyc


def test_drift_diagnostics_name_the_edge_core_and_seam():
    rep, _, _ = run_case("blade")
    enter, seam = sorted((x for x in rep["diagnostics"] if x["rule"] == "bbox_drift"), key=lambda x: -x["frame"])
    assert (enter["frame"], enter["edge"], enter["core_delta"], enter["protrusion"], enter["seam"]) == (
        2, "top", 0, [0, 3], False,
    )  # fmt: skip
    assert enter["edge_deltas"] == {"left": 0, "top": 3, "right": 0, "bottom": 0}
    assert (seam["frame"], seam["protrusion"], seam["seam"]) == (0, [3, 0], True)
    assert "bbox_drift: frame 4 (east/0) bbox moved 3px (top edge; core 0px; loop seam), limit 1" in rep["failures"]
    rep, _, _ = run_case("shifted")
    assert {(x["frame"], x["edge"], x["core_delta"]) for x in rep["diagnostics"]} == {(2, "top", 2), (3, "top", 2)}


def test_foot_slide_diagnostic_reports_foot_body():
    rep, _, _ = run_case("foot_slide")
    (dg,) = rep["diagnostics"]
    assert (dg["value"], dg["foot_x"], dg["foot_body"], dg["anchor_x"]) == (3.5, 15.5, 3.5, 12)
    assert rep["failures"] == [
        "foot_slide: frame 5 (east/1) foot 3.5px from anchor x 12 (3.5px from the body core), limit 3"
    ]
    assert all(r["foot_body"] == 0 for r in rep["frames"] if r["frame"] != 1 or r["direction"] != "east")


def test_ungated_drift_is_measured_and_warned():
    rep = validate(load("shifted"), **KW, gate_bbox_drift=False)
    assert rep["pass"] and keys(rep["warnings"]) == {("bbox_drift", "east", 2), ("bbox_drift", "east", 3)}
    assert [x["gated"] for x in rep["diagnostics"]] == [False, False] and not rep["limits"]["bbox_drift_gated"]


def test_chain_records_are_provenance_not_verdicts():
    pre = {d: [{"off_palette_before": 3, "fringe_removed": 1} for _ in range(M.FRAMES)] for d in DIRS}
    reg = {"east": {"shift_px": [1, 2], "clipped_px": [0, 0, 0, 0]}}
    rep = validate(load("clean"), **KW, pre=pre, registration=reg, limits_source="characters.toy")
    assert rep["pass"] and rep["frames"][0]["pre"] == pre["south"][0] and rep["registration"] == reg
    assert rep["chain"]["off_palette_before"] == 24 and "not proof" in rep["chain"]["note"]
    assert rep["limits"]["source"] == "characters.toy" and rep["limits"]["max_bbox_drift_px"] == 1


def test_limits_are_required():
    for k in vars(LIMITS):
        with pytest.raises(ValueError, match=k):
            validate(load("clean"), **(KW | {"limits": SimpleNamespace(**(vars(LIMITS) | {k: None}))}))
    with pytest.raises(ValueError, match="hover_x_drift_warn_px"):
        validate(load("clean"), **KW, anchor_mode="bbox_bottom")
    with pytest.raises(ValueError, match="anchor_mode 'rig'"):
        validate(load("clean"), **KW, anchor_mode="rig")


def test_foot_rule_is_per_anchor_mode():
    assert foot_slide("foot", 15.5, 12) == 3.5 and foot_slide("bbox_bottom", 15.5, 12) is None


def test_bbox_bottom_skips_foot_slide_and_warns_on_hover():
    """ADR-0008 F2: no foot_slide; the static clip bobs 0 px (warned), a slid foot is not a failure."""
    rep = validate(load("foot_slide"), **(KW | {"limits": HOVER, "anchor_mode": "bbox_bottom"}))
    assert rep["pass"] and rep["cycle"]["foot_slide_max"] is None and "ADR-0008 F2" in rep["notes"][0]
    assert all(r["foot_slide"] is None and r["foot_body"] is None for r in rep["frames"])
    assert warn_keys(rep) == {("hover_bob", "south"), ("hover_bob", "east")}
    # bobbing 0/1/2/1 px and drifting 0/2/4/2 px sideways: no low-bob warning, an x-drift warning (4 > 3)
    bob = []
    for i, dy, dx in zip(range(4), (0, 1, 2, 1), (0, 2, 4, 2), strict=True):
        f = M.figure(i)
        f[11, :7] = f[11, 17:] = 0  # no arms, so +4 px stays inside the padding
        bob.append(np.roll(np.roll(f, -dy, 0), dx, 1))
    lim = SimpleNamespace(**(vars(HOVER) | {"max_bbox_drift_px": 4}))
    rep = validate({"east": bob}, **(KW | {"limits": lim, "anchor_mode": "bbox_bottom", "directions": ["east"]}))
    assert (rep["directions"]["east"]["hover_x_drift_px"], rep["directions"]["east"]["hover_bob_px"]) == (4.0, 2.0)
    assert warn_keys(rep) == {("hover_x_drift", "east")}


# ---------------------------------------------------------------- CLI


@pytest.fixture
def profile(tmp_path) -> Path:
    pal = tmp_path / "palette.png"
    Image.fromarray(M.PALETTE[None]).save(pal)
    lim = {"max_foot_slide_px": 3, "max_bbox_drift_px": 1, "opaque_area_cv_warn": 0.1, "loop_closure_ratio_warn": 1.29}
    doc = {
        "id": "toy",
        "name": "Toy",
        "style": "pixel",
        "canvas": {"cell": "per_character", "gen_scale": 3, "padding": 2, "sheet_layout": "rows_by_direction"},
        "palette": {"source": "palette.png", "sha256": sha256_file(pal), "colours": len(M.PALETTE)}
        | {"max_colors_per_cell": M.MAX_COLOURS, "dither": "none"},
        "anchor": {"mode": "foot", "foot_y": "cell_minus_4", "anchor_x": "cell_half", "registration": "per_clip"},
        "alpha": {"threshold": 128, "bg_key": "#ff00ff", "fringe_passes": 1, "fringe_tolerance": 0},
        "directions": {"order": ["west", "south", "east"], "generate": DIRS, "mirror_animations": {"west": "east"}},
        "animations": {"idle": {"frames": 4, "fps": 5, "loop": True}},
        "validator": {"label": "toy defaults", **lim},
        "characters": {"hero": {"cell": 24, "anchor_mode": "foot", **lim}},
        "export": {"godot_project": "game", "sprites_dir": "res://sprites", "manifest": "characters.json"},
        "autonomy": {"allow_auto_approve": False, "max_seeds_per_attempt": 4, "max_attempts_per_animation": 3}
        | {"escalate_after": "ask"},
    }
    (tmp_path / "toy.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    return tmp_path / "toy.yaml"


def frames_dir(tmp_path: Path, case: str) -> Path:
    out = tmp_path / case
    out.mkdir()
    for d, fs in load(case).items():
        for i, f in enumerate(fs):
            Image.fromarray(f).save(out / f"{d}_{i}.png")
    return out


def test_cli_validate_exit_codes(profile, tmp_path, capsys):
    args = ["--profile", str(profile), "--character", "hero", "--anim", "idle"]
    assert main(["validate", str(frames_dir(tmp_path, "clean")), *args]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["pass"] and (rep["character"], rep["animation"]) == ("hero", "idle") and next(iter(rep)) == "pass"
    assert rep["limits"]["source"] == "characters.hero" and len(rep["provenance"]["inputs"]) == 8
    assert "registration provenance not supplied: clipping off the cell cannot be checked" in rep["warnings"]
    out = tmp_path / "report.json"
    assert main(["validate", str(frames_dir(tmp_path, "foot_slide")), *args, "--out", str(out)]) == 1
    assert keys(json.loads(out.read_text())["failures"]) == {("foot_slide", "east", 1)}
    capsys.readouterr()
    assert main(["validate", str(tmp_path / "clean"), *args, "--out", str(out)]) == 2  # never overwrites
    assert "never overwritten" in capsys.readouterr().err
    assert main(["validate", str(tmp_path / "clean"), *args[:-1], "attack"]) == 2
    (tmp_path / "clean" / "east_3.png").rename(tmp_path / "clean" / "east_7.png")
    assert main(["validate", str(tmp_path / "clean"), *args]) == 2
    assert "numbered [0, 1, 2, 7]" in capsys.readouterr().err
    assert main(["validate", str(tmp_path / "nowhere"), *args]) == 2


@pytest.mark.parametrize("clipped_px, expected_exit", [([0, 0, 0, 0], 0), ([0, 0, 7, 0], 1)])
def test_cli_validate_registration(profile, tmp_path, capsys, clipped_px, expected_exit):
    registration = {"east": {"shift_px": [0, 0], "clipped_px": clipped_px}}
    path = tmp_path / "registration.json"
    path.write_text(json.dumps(registration))
    args = [
        "validate",
        str(frames_dir(tmp_path, "clean")),
        "--profile",
        str(profile),
        "--character",
        "hero",
        "--anim",
        "idle",
        "--registration",
        str(path),
    ]
    assert main(args) == expected_exit
    rep = json.loads(capsys.readouterr().out)
    assert rep["pass"] is (expected_exit == 0) and rep["registration"] == registration
    assert not any("registration provenance not supplied" in w for w in rep["warnings"])
    assert keys(rep["failures"]) == ({("registration", "east", 2)} if expected_exit else set())


@pytest.mark.parametrize(
    "registration",
    [None, [], {"east": None}, {"east": {"clipped_px": [0]}}, {"east": {"clipped_px": [0, 0, -1, 0]}}],
)
def test_cli_validate_rejects_bad_registration(profile, tmp_path, capsys, registration):
    path = tmp_path / "registration.json"
    path.write_text(json.dumps(registration))
    args = [
        "validate",
        str(frames_dir(tmp_path, "clean")),
        "--profile",
        str(profile),
        "--character",
        "hero",
        "--anim",
        "idle",
        "--registration",
        str(path),
    ]
    assert main(args) == 2
    assert "registration must map clip directions" in capsys.readouterr().err


def test_read_frames_orders_by_profile_rows(tmp_path):
    d = frames_dir(tmp_path, "clean")
    (d / "contact.png").write_bytes((d / "east_0.png").read_bytes())  # other names are ignored
    clips, files = read_frames(d, ["north", "east", "south"])
    assert list(clips) == ["east", "south"] and len(files) == 8 and files[0].name == "east_0.png"


def test_opening_removes_thin_parts():
    m = np.zeros((12, 12), bool)
    m[2:9, 2:8] = True
    m[5, 8:12] = True  # 1 px arm
    assert np.array_equal(opening(m), np.pad(np.ones((7, 6), bool), ((2, 3), (2, 4))))


# ---------------------------------------------------------------- real Phase 1 data (private; skipped when absent)


def _real_profile():
    prof = load_profile(REPO / "profiles" / "twos-are-up.yaml")
    try:
        return prof, load_palette(prof)
    except Exception as e:  # the private game checkout holds the palette
        pytest.skip(f"palette not present: {e}")


@pytest.mark.skipif(not E_SETS.is_dir(), reason=f"Phase 1 Track E sets not present at {E_SETS}")
def test_real_track_e_sets_match_bound_reports():
    """Humanoids (anchor_mode foot, ADR-0008 H0): every set fails exactly as its bound report, frame for frame.
    relay_sentinel (bbox_bottom, F2): the bound failures were all foot_slide, so all four now pass the gates; seeds
    1 and 4 carry an x-drift warning and every seed a low-bob warning (validator study)."""
    prof, pal = _real_profile()
    reports = sorted(E_SETS.glob("*/*/set/processed/report.json"))
    assert len(reports) == 12
    seen = {}
    for rp in reports:
        bound = json.loads(rp.read_text())
        name, seed = rp.parts[-5], rp.parts[-4]
        spec = prof.character(name)
        anim = spec.animations[spec.locomotion]
        clips, files = read_frames(rp.parent / "processed", prof.directions.order)
        assert {p.name: sha256_file(p) for p in files} == {
            k: v for k, v in bound["provenance"]["outputs"].items() if k != "sheet.png"
        }  # the bound processed frames
        rep = validate(
            clips, cell=spec.cell, frames=anim.frames, directions=list(prof.directions.generate), palette=pal,
            foot_y=spec.foot_y, anchor_x=spec.anchor_x, padding=spec.padding, limits=spec.limits,
            max_colours=prof.palette.max_colors_per_cell, loop=anim.loop, anchor_mode=spec.anchor_mode,
            mirror=prof.directions.mirror_animations, registration=bound["registration"],
        )  # fmt: skip
        assert rep["pass"] is (not rep["failures"])
        bound_keys = keys(bound["failures"])
        if spec.anchor_mode == "foot":
            assert not rep["pass"] and keys(rep["failures"]) == bound_keys, (name, seed)
            for mine, theirs in zip(rep["frames"], bound["frames"], strict=True):
                assert (mine["direction"], mine["frame"], mine["foot_slide"], mine["bbox_drift"]) == (
                    theirs["direction"], theirs["frame"], theirs["foot_slide"], theirs["bbox_drift"],
                )  # fmt: skip
        else:
            assert {k[0] for k in bound_keys} <= {"foot_slide"} and rep["pass"], (name, seed)
        seen[(name, seed)] = (rep["pass"], {w for w, _ in warn_keys(rep) if w.startswith("hover")})
    assert [k for k, (ok, _) in seen.items() if ok] == [("relay_sentinel", s) for s in "1234"]
    hover = {s: seen[("relay_sentinel", s)][1] for s in "1234"}
    assert hover == {"1": {"hover_x_drift", "hover_bob"}, "2": {"hover_bob"}, "3": {"hover_bob"}} | {
        "4": {"hover_x_drift", "hover_bob"}
    }


@pytest.mark.skipif(not (GT / "relay_sentinel").is_dir(), reason=f"shipped reference clips not present at {GT}")
def test_shipped_relay_sentinel_triggers_no_hover_warning():
    prof, pal = _real_profile()
    spec = prof.character("relay_sentinel")
    anim = spec.animations["idle"]
    clips = {}
    for d in prof.directions.generate:
        fs = [np.asarray(Image.open(GT / "relay_sentinel" / f"gt_idle_{d}_{i}.png").convert("RGBA")) for i in range(4)]
        clips[d], _ = P.register_clip(fs, spec.cell[0])
    rep = validate(
        clips, cell=spec.cell, frames=anim.frames, directions=list(prof.directions.generate), palette=pal,
        foot_y=spec.foot_y, anchor_x=spec.anchor_x, padding=spec.padding, limits=spec.limits,
        max_colours=prof.palette.max_colors_per_cell, loop=anim.loop, anchor_mode=spec.anchor_mode,
    )  # fmt: skip
    assert rep["pass"] and not [w for w in rep["warnings"] if w.startswith("hover")], rep["warnings"]
