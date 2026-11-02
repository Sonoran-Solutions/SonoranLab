"""sonoranlab.validate + `sonoranlab validate` on tiny synthetic fixtures (tests/fixtures/dyl83, no game pixels).

Each defect fires its rule on exactly the intended frame and nothing else fires; anchor_mode foot keeps parity with
the calibrated Phase 1 validator (benchmark/tools/process.py). The real-data regression reads the 12 Phase 1 Track E
sets from SONORANLAB_WORK (default ~/sonoranlab-work) and skips when they are absent.
"""

import hashlib
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
from sonoranlab.register import register_clip
from sonoranlab.slice import pack_sheet
from sonoranlab.validate import foot_slide, frame_digest, opening, read_frames, validate

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


@pytest.mark.parametrize("case", ["clean", "off_palette", "semi_alpha", "touches_edge", "canvas", "colours", "fringe"])
def test_stills_share_cell_rules_without_clip_rules(case):
    _, clips, _ = run_case(case)
    still = clips["east"][2 if case in ("touches_edge", "colours", "fringe") else 1]
    rep = validate(load("clean"), stills={"east": still}, **KW)
    assert rep["pass"] is (case == "clean")
    assert len(rep["frames"]) == 8 and len(rep["stills"]) == 1
    assert all(": still east " in m for m in rep["failures"])
    assert rep["stills"][0]["ok"] is rep["pass"]
    assert rep["digests"]["still_east"] == frame_digest(still)
    assert len(rep["digests"]) == 9


def test_empty_still_and_unknown_direction():
    rep = validate(load("clean"), stills={"south": np.zeros((24, 24, 4), np.uint8)}, **KW)
    assert rep["failures"] == ["empty: still south has no opaque pixels"]
    with pytest.raises(ValueError, match="still direction 'unknown'"):
        validate(load("clean"), stills={"unknown": load("clean")["south"][0]}, **KW)


def test_still_does_not_need_foot_line_or_accessory():
    still = np.zeros((24, 24, 4), np.uint8)
    still[4:6, 4:6] = (*M.SKIN, 255)
    rep = validate(load("clean"), stills={"south": still}, **KW)
    assert rep["pass"] and not rep["warnings"]
    assert frame_digest(still) == hashlib.sha256(b"24x24x4:" + still.tobytes()).hexdigest()
    assert frame_digest(still) != frame_digest(still.reshape(12, 48, 4))


@pytest.mark.parametrize("case, exit_code", [("clean", 0), ("off_palette", 1)])
def test_cli_validate_stills(profile, tmp_path, capsys, case, exit_code):
    path = frames_dir(tmp_path, "clean")
    stills = tmp_path / "stills"
    stills.mkdir()
    Image.fromarray(load(case)["east"][1]).save(stills / "still_west.png")
    assert (
        main(
            [
                "validate",
                str(path),
                "--profile",
                str(profile),
                "--character",
                "hero",
                "--anim",
                "idle",
                "--stills",
                str(stills),
            ]
        )
        == exit_code
    )
    rep = json.loads(capsys.readouterr().out)
    assert rep["stills"][0]["direction"] == "west" and "still_west" in rep["digests"]
    assert "still_west.png" in rep["provenance"]["inputs"]


@pytest.fixture
def bound_export(profile, tmp_path):
    clips = load("clean")
    stills = {"west": clips["south"][0]}
    report = {
        **validate(clips, stills=stills, mirror={"west": "east"}, **KW),
        "character": "hero",
        "animation": "idle",
        "provenance": {"profile": {"path": str(profile), "sha256": sha256_file(profile)}},
    }
    assert report["pass"]
    sheet = pack_sheet(clips, (24, 24), ["west", "south", "east"], 4, mirror={"west": "east"}, stills=stills)
    png, rep = tmp_path / "sheet.png", tmp_path / "report.json"
    Image.fromarray(sheet).save(png)
    rep.write_text(json.dumps(report))
    argv = [
        "export",
        "godot",
        str(png),
        "--profile",
        str(profile),
        "--character",
        "hero",
        "--cell",
        "24",
        "--rows",
        "west,south,east",
        "--clip",
        "idle:1:4:5:loop",
        "--report",
        str(rep),
        "--out",
        str(tmp_path / "out"),
    ]
    return argv, sheet, report, png, rep


def test_cli_export_bound_asset(bound_export, profile, tmp_path, capsys):
    argv, _, report, _, rep = bound_export
    prov = tmp_path / "prov.json"
    prov.write_text(json.dumps({"job": "cli", "validator": {"pass": False}}))
    assert main([*argv, "--provenance", str(prov)]) == 0
    meta = json.loads((tmp_path / "out/hero/hero.sonoranlab.json").read_text())
    assert meta["provenance"] == {
        "job": "cli",
        "validator": {
            "pass": True,
            "report_sha256": hashlib.sha256(rep.read_bytes()).hexdigest(),
            "character": "hero",
            "animation": "idle",
            "frames_bound": len(report["digests"]),
            "profile_sha256": sha256_file(profile),
        },
    }
    assert main(argv) == 2
    assert "never overwrite" in capsys.readouterr().err


@pytest.mark.parametrize(
    "case, message",
    [
        ("pixel", "sheet east frame 3 does not match"),
        ("character", "report character"),
        ("animation", "report animation"),
        ("missing_character", "report character"),
        ("missing_animation", "report animation"),
        ("old_report", "report character"),
        ("no_digests", "no frame digests"),
        ("mirror", "sheet west frame 1 does not mirror east"),
        ("still", "sheet still west does not match"),
        ("extra_digest", "report digest east_4 has no matching"),
        ("rows", "must match the profile"),
        ("cell", "profile says [24, 24]"),
        ("multi_clip", "single --clip"),
        ("game", "inside the shipped game"),
    ],
)
def test_cli_export_refuses_unbound_assets(bound_export, tmp_path, capsys, case, message):
    argv, sheet, report, png, rep = bound_export
    if case == "pixel":
        sheet[2 * 24 + 10, 4 * 24 + 12] = (*M.SKIN, 255)
        # Guarantee one changed pixel, regardless of the fixture's colour at that location.
        sheet[2 * 24 + 10, 4 * 24 + 12, 0] ^= 1
    elif case == "mirror":
        sheet[10, 2 * 24 + 12, 0] ^= 1
    elif case == "still":
        sheet[10, 12, 0] ^= 1
    elif case in ("character", "animation"):
        report[case] = "another"
    elif case.startswith("missing_"):
        report.pop(case.removeprefix("missing_"))
    elif case == "old_report":
        report = {"pass": True}
    elif case == "no_digests":
        report.pop("digests")
    elif case == "extra_digest":
        report["digests"]["east_4"] = report["digests"]["east_0"]
    elif case in ("rows", "cell"):
        argv[argv.index("--" + case) + 1] = "east,south,west" if case == "rows" else "16"
    elif case == "multi_clip":
        argv += ["--clip", "cast:1:4:5:once"]
    elif case == "game":
        argv[-1] = str(tmp_path / "game/out")
    Image.fromarray(sheet).save(png)
    rep.write_text(json.dumps(report))
    assert main(argv) == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "out").exists() and not (tmp_path / "game/out").exists()


@pytest.mark.parametrize("report_text", [None, '{"pass": false}', "{}", '{"pass": 1}', '{"pass": "true"}', "[]", "bad"])
def test_cli_export_requires_passing_report(bound_export, tmp_path, capsys, report_text):
    argv, _, _, _, rep = bound_export
    if report_text is None:
        i = argv.index("--report")
        del argv[i : i + 2]
    else:
        rep.write_text(report_text)
    assert main(argv) == 2
    assert "export needs a passing validator report" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_cli_export_refuses_other_profile(bound_export, profile, tmp_path, capsys):
    """A report validated with another profile (here: a different warning limit) is not the same evidence."""
    argv, *_ = bound_export
    other = tmp_path / "other.yaml"
    doc = yaml.safe_load(profile.read_text())
    doc["validator"]["opaque_area_cv_warn"] = 0.5
    doc["characters"]["hero"]["opaque_area_cv_warn"] = 0.5
    other.write_text(yaml.safe_dump(doc, sort_keys=False))
    assert sha256_file(other) != sha256_file(profile)
    argv[argv.index("--profile") + 1] = str(other)
    assert main(argv) == 2
    assert "report was validated with a different profile" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("provenance", [{}, {"profile": {}}, None])
def test_cli_export_refuses_report_without_profile_provenance(bound_export, tmp_path, capsys, provenance):
    argv, _, report, _, rep = bound_export
    if provenance is None:
        report.pop("provenance")
    else:
        report["provenance"] = provenance
    rep.write_text(json.dumps(report))
    assert main(argv) == 2
    assert "report has no profile provenance" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "clip, message",
    [
        ("idle:1:4:60:loop", "--clip fps 60 does not match the profile's idle fps 5;"),
        ("idle:1:4:5:once", "--clip loop False does not match the profile's idle loop True"),
        ("idle:1:4:5:loop:2", "--clip loop_from 2 does not match the profile's idle loop_from None"),
        ("idle:1:3:5:loop", "--clip frames 3 does not match the profile's idle frames 4"),
    ],
)
def test_cli_export_refuses_altered_playback(bound_export, tmp_path, capsys, clip, message):
    argv, *_ = bound_export
    argv[argv.index("--clip") + 1] = clip
    assert main(argv) == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_cli_export_refuses_clip_that_is_not_a_profile_animation(bound_export, tmp_path, capsys):
    argv, _, report, _, rep = bound_export
    argv[argv.index("--clip") + 1] = "cast:1:4:5:loop"
    report["animation"] = "cast"
    rep.write_text(json.dumps(report))
    assert main(argv) == 2
    assert "--clip 'cast' is not a clip of hero (idle)" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("inside", ["game", "extra"])
def test_cli_export_forbids_game_and_extra_dirs_together(bound_export, tmp_path, capsys, inside):
    """`--forbid-under` adds to the profile's game directory; it never replaces it."""
    argv, *_ = bound_export
    extra = tmp_path / "extra"
    extra.mkdir()
    game = str(Path(argv[argv.index("--profile") + 1]).parent / "game")
    argv[-1] = str(tmp_path / inside / "out")
    assert main([*argv, "--forbid-under", str(extra)]) == 2
    assert f"inside the shipped game {tmp_path / inside}" in capsys.readouterr().err
    assert not (tmp_path / "game/out").exists() and not (tmp_path / "extra/out").exists()
    assert not Path(game, "out").exists() and not (tmp_path / "out").exists()


def test_cli_export_requires_profile(bound_export):
    argv, *_ = bound_export
    i = argv.index("--profile")
    del argv[i : i + 2]
    with pytest.raises(SystemExit) as error:
        main(argv)
    assert error.value.code == 2


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


# ---------------------------------------------------------------- validator_mode fall (ADR-0008 fall addendum)

FALL_PAL = np.array([(17, 13, 12), (200, 40, 40)], np.uint8)
FALL_LIMITS = SimpleNamespace(
    max_foot_slide_px=0.25, max_bbox_drift_px=2, opaque_area_cv_warn=10.0, loop_closure_ratio_warn=1.29
)
# (x0, x1, y0) of a body standing on row 28 of a 32 px cell, tipping over: the top drops 3 px a frame (> the 2 px
# limit) and the half-pixel centre of the wider poses moves the foot 0.5 px (> 0.25); bottom and core centre stay.
FALL_POSES = [(14, 17, 13), (13, 18, 16), (12, 20, 19), (10, 21, 22), (8, 23, 25), (8, 23, 25)]


def fall_frame(x0: int, x1: int, y0: int, colour: int = 1) -> np.ndarray:
    f = np.zeros((32, 32, 4), np.uint8)
    f[y0:29, x0 : x1 + 1] = (*FALL_PAL[colour], 255)
    return f


def fall_validate(frames: list[np.ndarray], mode: str | None = "fall", **kw) -> dict:
    args = {
        "cell": (32, 32),
        "frames": len(frames),
        "directions": ["east"],
        "palette": FALL_PAL,
        "foot_y": 28,
        "anchor_x": 16,
        "padding": 2,
        "limits": FALL_LIMITS,
        "max_colours": 4,
        "loop": False,
        "validator_mode": mode,
    }
    return validate({"east": frames}, **(args | kw))


def test_fall_mode_passes_a_falling_figure_the_default_rules_reject():
    frames = [fall_frame(*p) for p in FALL_POSES]
    default = fall_validate(frames, mode=None)
    assert {m.split(":")[0] for m in default["failures"]} == {"bbox_drift", "foot_slide"}  # what a fall must do
    assert "validator_mode" not in default and "fall_hold_max_delta" not in default["limits"]  # default unchanged
    rep = fall_validate(frames)
    assert rep["pass"], rep["failures"]
    assert rep["validator_mode"] == "fall" and rep["limits"]["fall_hold_max_delta"] == 0.02
    assert any("top edge moved 3px" in w and "not gated" in w for w in rep["warnings"])  # reported, not gated
    fall = rep["directions"]["east"]["fall"]
    assert fall == {
        "drift_max": 0.5,
        "foot_slide_frame0": 0.0,
        "hold_delta": 0.0,
        "final_inside": True,
        "static": False,
    }
    assert rep["directions"]["east"]["foot_slide_max"] == 0.5  # later frames still reported


def test_fall_mode_still_gates_frame0_foot_bottom_edge_and_core_centre():
    shifted = [fall_frame(15, 18, 13), *[fall_frame(*p) for p in FALL_POSES[1:]]]
    assert any(m.startswith("foot_slide: frame 0 ") for m in fall_validate(shifted)["failures"])
    frames = [fall_frame(*p) for p in FALL_POSES]
    frames[3] = fall_frame(13, 24, 22)  # core centre jumps 2.5 px right, then back
    msgs = [m for m in fall_validate(frames)["failures"] if m.startswith("bbox_drift")]
    assert len(msgs) == 2 and all("(fall)" in m for m in msgs)


@pytest.mark.parametrize(
    "case, rule",
    [("clipped", "fall_final"), ("in_padding", "fall_final"), ("not_settled", "fall_hold"), ("static", "static_clip")],
)
def test_fall_gates_fail(case, rule):
    frames = [fall_frame(*p) for p in FALL_POSES]
    kw = {}
    if case == "clipped":
        kw["registration"] = {"east": {"clipped_px": [0, 0, 0, 0, 0, 3], "shift_px": [0, 0]}}
    elif case == "in_padding":
        frames[5] = fall_frame(8, 23, 25)
        frames[5][25:29, 30] = (*FALL_PAL[0], 255)  # an arm reaches into the right padding
    elif case == "not_settled":
        frames[5] = fall_frame(10, 25, 25)  # the body is still sliding: its shape moves 2 px
    else:
        frames = [fall_frame(*FALL_POSES[0])] * 6
    rep = fall_validate(frames, **kw)
    assert not rep["pass"] and any(m.startswith(f"{rule}: east") for m in rep["failures"]), rep["failures"]


def test_fall_mode_needs_a_non_looping_clip():
    with pytest.raises(ValueError, match="fall needs loop=False"):
        fall_validate([fall_frame(*p) for p in FALL_POSES], loop=True)
    with pytest.raises(ValueError, match="validator_mode"):
        fall_validate([fall_frame(*p) for p in FALL_POSES], mode="hold")


def test_fall_mode_gates_frame0_even_when_frame0_is_not_a_contact_frame():
    # frame 0 stands 2 px higher than the later poses (so it is not on the clip's lowest foot line) and 1 px off the
    # anchor: the frame-0 gate must still fire (codex review of c9bddda)
    frames = [fall_frame(15, 18, 11), *[fall_frame(*p) for p in FALL_POSES[1:]]]
    frames[0] = np.roll(frames[0], -2, axis=0)
    rep = fall_validate(frames)
    assert any(m.startswith("foot_slide: frame 0 ") for m in rep["failures"]), rep["failures"]


def test_fall_hold_ignores_colour_shimmer_on_a_resting_body():
    # Track E re-quantises a resting body differently frame to frame: same shape, every pixel recoloured -> held
    frames = [fall_frame(*p) for p in FALL_POSES]
    frames[5] = fall_frame(8, 23, 25, colour=0)
    rep = fall_validate(frames)
    assert rep["pass"], rep["failures"]
    assert rep["directions"]["east"]["fall"]["hold_delta"] == 0.0


def _fall_raw(x1s: list[int], width: int = 48) -> list[np.ndarray]:
    """Raw (pre-registration) frames wider than the 32 px cell: frame 0 stands at x 14..17 (centre 16 = anchor_x),
    then the body tips over to the right, growing to x 14..x1, bottom row 28 throughout; the last pose is held."""
    tops = [13, 16, 19, 22, 25, 25]
    out = []
    for x1, y0 in zip(x1s, tops, strict=True):
        f = np.zeros((32, width, 4), np.uint8)
        f[y0:29, 14 : x1 + 1] = (*FALL_PAL[1], 255)
        out.append(f)
    return out


def _fit_and_validate(raw: list[np.ndarray], limits=None) -> dict:
    """pipeline.run's fall path: frame-0 anchor with fit_padding, then the fall validator with the registration."""
    reg, info = register_clip(raw, (32, 32), foot_y=28, anchor_x=16, anchor_frame=0, fit_padding=2)
    limits = limits or SimpleNamespace(
        max_foot_slide_px=5, max_bbox_drift_px=8, opaque_area_cv_warn=10.0, loop_closure_ratio_warn=1.29
    )
    return fall_validate(reg, registration={"east": info}, limits=limits)


def test_fall_fit_shift_passes_with_a_start_offset_warning():
    """Frame-0 anchoring would clip the lying body at the right edge (x 33 > 29); fit, then anchor moves the clip 4 px
    left, every frame lies inside, and the 4 px jump from the still's anchor is reported for a 1x judgement."""
    raw = _fall_raw([17, 20, 24, 28, 33, 33])
    unfit, info0 = register_clip(raw, (32, 32), foot_y=28, anchor_x=16, anchor_frame=0)
    before = fall_validate(
        unfit,
        registration={"east": info0},
        limits=SimpleNamespace(**vars(FALL_LIMITS) | {"max_foot_slide_px": 5, "max_bbox_drift_px": 8}),
    )
    assert {m.split(":")[0] for m in before["failures"]} >= {"touches_edge", "fall_final", "registration"}
    rep = _fit_and_validate(raw)
    assert rep["pass"], rep["failures"]
    assert rep["registration"]["east"]["fit"] == {"padding": 2, "offset_px": -4, "fits": True}
    assert rep["directions"]["east"]["fall"]["start_offset_px"] == -4
    assert rep["directions"]["east"]["fall"]["foot_slide_frame0"] == 4.0  # frame 0 now stands 4 px left of anchor
    (warn,) = [w for w in rep["warnings"] if w.startswith("fall_start_offset")]
    assert "east death start offset -4 px from the still's anchor" in warn


def test_fall_fit_cannot_rescue_an_unfittable_clip_or_hide_a_large_jump():
    wide = _fall_raw([17, 22, 28, 36, 47, 47])  # union x 14..47 = 34 px > the 28 px inside the padding
    rep = _fit_and_validate(wide)
    assert not rep["pass"] and rep["registration"]["east"]["fit"] == {"padding": 2, "offset_px": 0, "fits": False}
    assert {m.split(":")[0] for m in rep["failures"]} >= {"touches_edge", "fall_final", "registration"}
    assert not [w for w in rep["warnings"] if w.startswith("fall_start_offset")]
    far = _fall_raw([17, 22, 28, 33, 38, 38])  # fits only with a 9 px shift: frame 0's foot_slide gate (5) fails it
    rep = _fit_and_validate(far)
    assert rep["registration"]["east"]["fit"]["offset_px"] == -9
    assert [m.split(":")[0] for m in rep["failures"]] == ["foot_slide"]
    assert any(w.startswith("fall_start_offset") for w in rep["warnings"])


def test_fall_report_without_a_fit_record_is_unchanged():
    """Composed/finalized re-validation of older fall reports (no `fit`) and every non-fall report keep their keys."""
    rep = fall_validate([fall_frame(*p) for p in FALL_POSES], registration={"east": {"clipped_px": [0] * 6}})
    assert "start_offset_px" not in rep["directions"]["east"]["fall"]
    assert not [w for w in rep["warnings"] if w.startswith("fall_start_offset")]
