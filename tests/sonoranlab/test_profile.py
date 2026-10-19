"""sonoranlab.profile + `sonoranlab profile` on a synthetic profile built in tmp dirs (no game files needed).

The real profiles/twos-are-up.yaml is checked twice: schema only (everywhere, CI) and with path checks (skips without
the private game checkout at ~/Projects/TwosAreUp).
"""

import copy
import hashlib
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml
from PIL import Image

from sonoranlab.cli import main
from sonoranlab.profile import HOVER_KEYS, LIMIT_KEYS, ProfileError, check_profile, load_palette, load_profile

REPO = Path(__file__).resolve().parents[2]
REAL = REPO / "profiles" / "twos-are-up.yaml"
GAME = Path("~/Projects/TwosAreUp").expanduser()
COLOURS = [(17, 13, 12), (204, 34, 34), (230, 180, 140), (40, 60, 200)]  # synthetic, not the game's palette

BASE = {
    "id": "toy",
    "name": "Toy",
    "style": "pixel",
    "canvas": {"cell": "per_character", "gen_scale": 3, "padding": 2, "sheet_layout": "rows_by_direction"},
    "palette": {"source": "palette.png", "sha256": "", "colours": 4, "max_colors_per_cell": 4, "dither": "none"},
    "anchor": {"mode": "foot", "foot_y": "cell_minus_4", "anchor_x": "cell_half", "registration": "per_clip"},
    "alpha": {"threshold": 128, "bg_key": "#ff00ff", "fringe_passes": 1, "fringe_tolerance": 0},
    "directions": {
        "order": ["west", "south", "east"],
        "generate": ["south", "east"],
        "mirror_animations": {"west": "east"},
    },
    "animations": {
        "idle": {"frames": 2, "fps": 5, "loop": True},
        "run": {"frames": 3, "fps": 10, "loop": True, "track_default": "E", "template": "templates/run"},
        "channel": {"frames": 4, "fps": 8, "loop": False, "loop_from": 2},
    },
    "validator": {
        "label": "toy defaults",
        "max_foot_slide_px": 30,
        "max_bbox_drift_px": 16,
        "opaque_area_cv_warn": 0.13,
        "loop_closure_ratio_warn": 1.29,
    },
    "characters": {
        "hero": {
            "cell": 16,
            "anchor_mode": "foot",
            "animations": {"run": {"frames": 4}},
            "max_foot_slide_px": 3,
            "max_bbox_drift_px": 2,
            "opaque_area_cv_warn": 0.11,
            "loop_closure_ratio_warn": 1.18,
        },
        "floater": {
            "cell": [24, 16],
            "anchor_mode": "bbox_bottom",
            "locomotion": "idle",
            "max_foot_slide_px": 5,
            "max_bbox_drift_px": 4,
            "opaque_area_cv_warn": 0.1,
            "loop_closure_ratio_warn": 1.29,
            "hover_x_drift_warn_px": 3,
            "hover_min_bob_px": 1.5,
        },
    },
    "export": {"godot_project": "game", "sprites_dir": "res://sprites", "manifest": "characters.json"},
    "autonomy": {
        "allow_auto_approve": False,
        "max_seeds_per_attempt": 4,
        "max_attempts_per_animation": 3,
        "escalate_after": "ask",
    },
}


@pytest.fixture
def make(tmp_path):
    """make(edit) -> path of a profile in tmp_path: palette PNG, run template (3 and 4 frames), game dir, + edit."""
    pal = tmp_path / "palette.png"
    Image.fromarray(np.array([COLOURS], np.uint8)).save(pal)
    (tmp_path / "templates" / "run").mkdir(parents=True)
    tpl = {"directions": ["south", "east"], "frame_sets": {3: {}, 4: {}}}
    (tmp_path / "templates" / "run" / "template.yaml").write_text(yaml.safe_dump(tpl))
    (tmp_path / "game" / "sprites").mkdir(parents=True)
    (tmp_path / "game" / "sprites" / "characters.json").write_text("{}")

    def _make(edit=None) -> Path:
        d = copy.deepcopy(BASE)
        d["palette"]["sha256"] = hashlib.sha256(pal.read_bytes()).hexdigest()
        if edit:
            edit(d)
        out = tmp_path / "profile.yaml"
        out.write_text(yaml.safe_dump(d, sort_keys=False))
        return out

    return _make


def problems(path: Path) -> str:
    return "\n".join(check_profile(path))


def test_valid_profile_loads(make):
    p = load_profile(make(), check_paths=True)
    assert check_profile(make()) == []
    assert p.canvas.cell is None and p.directions.mirror_animations == {"west": "east"}
    assert p.animations["run"].template == make().parent / "templates" / "run"


def test_known_character_resolves_with_overrides(make):
    hero = load_profile(make()).character("hero")
    assert (hero.cell, hero.foot_y, hero.anchor_x) == ((16, 16), 12, 8)
    assert hero.animations["run"].frames == 4 and hero.animations["run"].fps == 10  # override merged
    assert hero.limits.max_foot_slide_px == 3 and not hero.uses_defaults
    assert hero.limits_source == "characters.hero" and hero.locomotion == "run"
    floater = load_profile(make()).character("floater")
    assert (floater.cell, floater.foot_y, floater.anchor_x, floater.anchor_mode) == ((24, 16), 12, 12, "bbox_bottom")
    assert floater.locomotion == "idle" and floater.animations["run"].frames == 3


@pytest.mark.parametrize(
    "clip, override",
    [
        ("idle", {"fps": 7.5}),
        ("idle", {"loop": False}),
        ("channel", {"loop_from": 1}),
        ("channel", {"hold_last": True}),
        ("idle", {"track_default": "D"}),
        ("channel", {"loop": True, "loop_from": None}),
        ("run", {"track_default": None, "template": None}),
    ],
)
def test_partial_animation_overrides(make, clip, override):
    path = make(lambda d: d["characters"]["hero"].update(animations={clip: override}))
    p = load_profile(path)
    resolved = p.character("hero").animations[clip]
    for key, value in override.items():
        assert getattr(resolved, key) == value
    assert resolved.frames == p.animations[clip].frames


@pytest.mark.parametrize("override, field", [({"loop": True}, "loop_from"), ({"frames": 2}, "loop_from")])
def test_partial_animation_rechecks_merged_rules(make, override, field):
    path = make(lambda d: d["characters"]["hero"].update(animations={"channel": override}))
    assert f"characters.hero.animations.channel.{field}:" in problems(path)
    path = make(lambda d: d["characters"]["hero"].update(animations={"idle": {"hold_last": True}}))
    assert "characters.hero.animations.idle.hold_last: needs loop: false" in problems(path)


NUMERIC_FIELDS = [
    *(f"validator.{key}" for key in LIMIT_KEYS + HOVER_KEYS),
    *(f"characters.hero.{key}" for key in LIMIT_KEYS),
    *(f"characters.floater.{key}" for key in HOVER_KEYS),
    "animations.idle.fps",
    "characters.hero.animations.run.fps",
    "canvas.density_px_per_m",
    "processing.key_tol",
]


@pytest.mark.parametrize("field", NUMERIC_FIELDS)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), None, True, "1"])
def test_numeric_fields_reject_invalid_values(make, capsys, field, value):
    def edit(d):
        d["processing"] = {
            "version": "processing-v1",
            "downscaler": "nearest",
            "thickness": 0,
            "despill_px": 0,
            "key_tol": 48,
        }
        row = d
        *parents, key = field.split(".")
        for parent in parents:
            row = row[parent]
        row[key] = value

    path = make(edit)
    assert f"{field}:" in problems(path)
    assert main(["profile", "check", str(path)]) == 2
    assert f"{field}:" in capsys.readouterr().out


@pytest.mark.parametrize("where", ["validator", "characters.hero"])
@pytest.mark.parametrize("key", LIMIT_KEYS[:2])
@pytest.mark.parametrize("value", [0, -1])
def test_validator_gates_must_be_positive(make, where, key, value):
    def edit(d):
        row = d["validator"] if where == "validator" else d["characters"]["hero"]
        row[key] = value

    assert f"{where}.{key}:" in problems(make(edit))


@pytest.mark.parametrize("where", ["anchor", "hero"])
def test_rig_anchor_profile_check(make, capsys, where):
    def edit(d):
        if where == "anchor":
            d["anchor"]["mode"] = "rig"
        else:
            d["characters"]["hero"]["anchor_mode"] = "rig"

    path = make(edit)
    field = "anchor.mode" if where == "anchor" else "characters.hero.anchor_mode"
    expected = f"{field}: rig anchors need Track D per-frame anchors and a rig validator rule; not supported in Phase 2"
    assert expected in problems(path)
    assert main(["profile", "check", str(path)]) == 2
    assert expected in capsys.readouterr().out


def test_unknown_character_uses_labelled_defaults(make):
    p = load_profile(make())
    with pytest.raises(ProfileError, match="per_character; pass its cell"):
        p.character("newcomer")
    spec = p.character("newcomer", cell=20)
    assert spec.uses_defaults and spec.limits_source == "toy defaults"
    assert spec.limits.max_foot_slide_px == 30 and spec.limits.loop_closure_ratio_warn == 1.29
    assert (spec.foot_y, spec.anchor_x) == (16, 10)
    with pytest.raises(ProfileError, match="caller asked for"):
        p.character("hero", cell=20)


@pytest.mark.parametrize(
    "edit, expected",
    [
        (lambda d: d["alpha"].update(bg_key="#ff00f"), "alpha.bg_key: '#ff00f' is not a #rrggbb hex colour"),
        (lambda d: d["alpha"].pop("threshold"), "alpha.threshold: missing"),
        (lambda d: d["anchor"].update(foot_y="cell_minus_1"), "outside rows 2..13 of the 16x16 cell"),
        (lambda d: d["anchor"].update(foot_y=40), "characters.hero: anchor.foot_y 40 puts the foot line on row 40"),
        (lambda d: d["anchor"].update(foot_zone=[2, 8]), "anchor.foot_y row 12 is outside anchor.foot_zone [2, 8]"),
        (lambda d: d["anchor"].update(foot_y="feet"), "anchor.foot_y: 'feet' is neither a row nor cell_minus_<N>"),
        (lambda d: d["animations"]["run"].update(frames=5), "animations.run.frames: 5 does not match template"),
        (
            lambda d: d["characters"]["hero"]["animations"]["run"].update(frames=7),
            "characters.hero.animations.run.frames: 7 does not match template",
        ),
        (lambda d: d["animations"]["run"].update(template="templates/walk"), "has no readable template.yaml"),
        (lambda d: d["directions"]["generate"].append("north"), "directions.generate: 'north' is not in"),
        (lambda d: d["directions"]["mirror_animations"].update(up="east"), "mirror_animations: 'up' is not in"),
        (lambda d: d["directions"]["mirror_animations"].update(west="north"), "source 'north' is not generated"),
        (lambda d: d["directions"]["mirror_animations"].clear(), "'west' is neither generated nor mirrored"),
        (lambda d: d["palette"].update(dither="ordered"), "palette.dither: 'ordered' is not supported"),
        (lambda d: d["palette"].update(sha256="0" * 64), "palette.sha256: "),
        (lambda d: d["palette"].update(colours=5), "palette.colours: 5, but"),
        (lambda d: d["palette"].update(source="nope.png"), "palette.source: "),
        (lambda d: d["export"].update(godot_project="missing"), "export.godot_project: "),
        (lambda d: d["export"].update(manifest="other.json"), "export.manifest: "),
        (lambda d: d["validator"].update(max_foot_slide_px="calibrate"), "validator.max_foot_slide_px: 'calibrate'"),
        (lambda d: d["validator"].pop("max_bbox_drift_px"), "validator.max_bbox_drift_px: missing"),
        (lambda d: d["characters"]["hero"].pop("opaque_area_cv_warn"), "characters.hero.opaque_area_cv_warn: missing"),
        (lambda d: d["characters"]["hero"].pop("cell"), "characters.hero.cell: missing (canvas.cell is per_character)"),
        (
            lambda d: d["characters"]["floater"].pop("hover_min_bob_px"),
            "characters.floater.hover_min_bob_px: missing (anchor_mode bbox_bottom needs the hover warnings",
        ),
        (lambda d: d["anchor"].update(mode="bbox_bottom"), "validator.hover_x_drift_warn_px: missing (anchor_mode"),
        (lambda d: d["characters"]["hero"].update(locomotion="walk"), "characters.hero.locomotion: 'walk' is not"),
        (lambda d: d["characters"]["hero"]["animations"].update(cut={"frames": 2}), "animations.cut: not a clip"),
        (
            lambda d: d["animations"]["run"].update(track_default="pending ADR-0004"),
            "animations.run.track_default: 'pending ADR-0004' is not one of A, B, C, D, E",
        ),
        (lambda d: d["animations"]["channel"].update(loop_from=4), "animations.channel.loop_from: 4 needs loop"),
        (lambda d: d["animations"]["idle"].update(hold_last=True), "animations.idle.hold_last: needs loop: false"),
        (lambda d: d["canvas"].update(margin=1), "canvas.margin: unknown key"),
        (lambda d: d.pop("export"), "export: missing section"),
    ],
)
def test_specific_errors(make, edit, expected):
    assert expected in problems(make(edit))


def test_load_profile_raises_with_every_problem(make):
    def edit(d):
        d["alpha"]["bg_key"] = "magenta"
        d["validator"]["opaque_area_cv_warn"] = "calibrate"

    with pytest.raises(ProfileError) as e:
        load_profile(make(edit))
    assert len(e.value.problems) == 2


def test_bg_key_in_palette_refused(make):
    assert "alpha.bg_key: #110d0c is a palette colour" in problems(make(lambda d: d["alpha"].update(bg_key="#110d0c")))


def test_load_palette(make):
    pal = load_palette(load_profile(make()))
    assert pal.dtype == np.uint8 and pal.shape == (4, 3) and {tuple(c) for c in pal.tolist()} == set(COLOURS)
    p = load_profile(make(lambda d: d["palette"].update(sha256="f" * 64)))  # schema-valid, file mismatched
    with pytest.raises(ProfileError, match=r"palette\.sha256: .* hashes to"):
        load_palette(p)


def test_path_checks_can_be_skipped_and_loading_writes_nothing(make):
    path = make(lambda d: d["export"].update(godot_project="/nonexistent/game"))
    before = sorted(p for p in path.parent.rglob("*"))
    assert check_profile(path, check_paths=False) == []
    assert any("export.godot_project" in m for m in check_profile(path))
    assert sorted(p for p in path.parent.rglob("*")) == before


def test_cli_exit_codes(make, capsys):
    good = make()
    assert main(["profile", "check", str(good)]) == 0
    assert main(["profile", "show", str(good), "--character", "hero"]) == 0
    assert '"foot_y": 12' in capsys.readouterr().out
    assert main(["profile", "show", str(good), "--character", "newcomer"]) == 2
    bad = make(lambda d: d["alpha"].update(bg_key="pink"))
    assert main(["profile", "check", str(bad)]) == 2
    assert "alpha.bg_key: 'pink'" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        main(["profile"])
    assert e.value.code == 2


def test_profile_import_does_not_load_torch():
    code = "import sys, sonoranlab.profile, sonoranlab.cli; print('torch' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout == "False\n"


def test_real_profile_schema():
    p = load_profile(REAL)
    assert check_profile(REAL, check_paths=False) == []
    samurai, sentinel = p.character("samurai"), p.character("relay_sentinel")
    assert samurai.cell == (184, 184) and samurai.foot_y == 180 and samurai.animations["run"].frames == 7
    assert (samurai.limits.max_foot_slide_px, samurai.limits.max_bbox_drift_px) == (30, 16)
    assert p.character("oathguard").limits.max_bbox_drift_px == 8
    assert sentinel.anchor_mode == "bbox_bottom" and sentinel.locomotion == "idle"
    assert p.animations["run"].track_default == "E"
    unknown = p.character("squire", cell=120)
    assert unknown.uses_defaults and unknown.limits.max_foot_slide_px == 30 and "all" in unknown.limits_source
    assert unknown.limits.max_bbox_drift_px == 25 and "ADR-0008 H4" in unknown.limits_source  # H4: pooled shipped runs
    assert (sentinel.limits.hover_x_drift_warn_px, sentinel.limits.hover_min_bob_px) == (3, 1.5)  # ADR-0008 F2


@pytest.mark.skipif(not GAME.is_dir(), reason=f"private game checkout {GAME} absent: palette/export paths unchecked")
def test_real_profile_with_paths():
    assert check_profile(REAL) == []
    assert load_palette(load_profile(REAL)).shape == (255, 3)
