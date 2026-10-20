"""Synthetic asymmetric repair interchange, with no private art, editor, GPU, or network required."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import yaml
from PIL import Image, PngImagePlugin

from sonoranlab.artifacts import sha256_file
from sonoranlab.cli import main
from sonoranlab.profile import load_profile
from sonoranlab.repair import EditorUnavailable, Refused, runtime
from sonoranlab.repair import workspace as ws
from sonoranlab.repair.compare import compare, diff, load_rgba, png_chunks, read_json

ORDER = ["north", "north-west", "west", "south-west", "south", "south-east", "east", "north-east"]
GENERATE = ["north", "north-east", "east", "south-east", "south"]
MIRROR = {"north-west": "north-east", "west": "east", "south-west": "south-east"}
PALETTE = [(20 + i * 20, 30 + i * 10, 40 + i * 5) for i in range(8)]


def hashes(root):
    return {p.relative_to(root).as_posix(): sha256_file(p) for p in root.rglob("*") if p.is_file()}


def write(path, doc):
    path.write_text(json.dumps(doc))


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("SONORANLAB_REPAIR_ROOT", str(tmp_path))
    pal = tmp_path / "palette.png"
    Image.fromarray(np.array([PALETTE], np.uint8)).save(pal)
    limits = {
        "max_foot_slide_px": 2,
        "max_bbox_drift_px": 2,
        "opaque_area_cv_warn": 0.2,
        "loop_closure_ratio_warn": 1.5,
    }
    profile = {
        "id": "toy",
        "name": "Toy",
        "style": "pixel",
        "canvas": {"cell": "per_character", "gen_scale": 2, "padding": 1, "sheet_layout": "rows_by_direction"},
        "palette": {
            "source": "palette.png",
            "sha256": sha256_file(pal),
            "colours": 8,
            "max_colors_per_cell": 8,
            "dither": "none",
        },
        "anchor": {"mode": "foot", "foot_y": "cell_minus_4", "anchor_x": "cell_half", "registration": "per_clip"},
        "alpha": {"threshold": 128, "bg_key": "#ff00ff", "fringe_passes": 1, "fringe_tolerance": 0},
        "directions": {"order": ORDER, "generate": GENERATE, "mirror_animations": MIRROR},
        "animations": {"idle": {"frames": 2, "fps": 5, "loop": True}},
        "validator": {"label": "toy", **limits},
        "characters": {"hero": {"cell": 16, **limits}},
        "export": {"godot_project": "game", "sprites_dir": "res://sprites", "manifest": "characters.json"},
        "autonomy": {
            "allow_auto_approve": False,
            "max_seeds_per_attempt": 1,
            "max_attempts_per_animation": 1,
            "escalate_after": "ask",
        },
    }
    path = tmp_path / "toy.yaml"
    path.write_text(yaml.safe_dump(profile))
    (tmp_path / "game").mkdir()
    prof = load_profile(path)
    job = tmp_path / "job"
    (job / "raw").mkdir(parents=True)
    (job / "processed/05_registered").mkdir(parents=True)
    (job / "export/hero").mkdir(parents=True)
    for n, d in enumerate(ORDER):
        f = np.zeros((16, 16, 4), np.uint8)
        f[3:6, 2 + n] = (*PALETTE[n], 255)  # Unique, asymmetric *true* west stills.
        f[0, 0] = (15, 16, 17, 0)  # Hidden RGB does not affect equality.
        Image.fromarray(f).save(job / "raw" / f"still_{d}.png")
    for n, d in enumerate(GENERATE):
        for i in range(2):
            f = np.zeros((16, 16, 4), np.uint8)
            f[4 + i : 8, 2 + n] = (*PALETTE[n], 255)
            Image.fromarray(f).save(job / "processed/05_registered" / f"{d}_{i}.png")
    write(job / "report.json", {"pass": True})
    write(job / "review.json", {"verdict": "pass"})
    (job / "export/hero/hero.tres").write_text("synthetic export")
    outputs = {k: v for k, v in hashes(job).items() if k.startswith(("processed/", "export/")) or k == "report.json"}
    write(
        job / "manifest.json",
        {
            "character": "hero",
            "animation": "idle",
            "profile": {"sha256": sha256_file(path)},
            "result": {"pass": True, "exported": True},
            "outputs": outputs,
            "inputs": {p.name: sha256_file(p) for p in (job / "raw").iterdir()},
        },
    )
    return job, prof, tmp_path / "repair"


def prepare(fixture, converter=None):
    job, profile, repair = fixture
    ws.prepare(job, profile, "hero", "idle", repair, converter=converter or ws.FakeConverter())
    return repair


def export(repair):
    return Path(ws.export(repair, converter=ws.FakeConverter())["export"])


def test_unmodified_round_trip_all_stills_and_parent_immutable(fixture):
    job, profile, repair = fixture
    before = hashes(job)
    prepare(fixture)
    record = read_json(repair / "repair.json")
    assert record["schema"] == "sonoranlab.repair/1"
    assert record["profile"]["sha256"] == sha256_file(profile.path)
    assert record["parent_hashes"] == {
        k: before[k] for k in ("report.json", "manifest.json", "review.json", "export/hero/hero.tres")
    }
    mapping = read_json(repair / "mapping.json")
    assert [mapping[str(n)]["direction"] for n in range(1, 9)] == ORDER
    assert all(mapping[str(n)]["duration_ms"] == 100 for n in range(1, 9))
    assert all(mapping[str(n)]["duration_ms"] == 200 for n in range(9, 19))
    assert [mapping[str(n)]["direction"] for n in range(9, 19)] == [d for d in GENERATE for _ in range(2)]
    assert record["metadata"]["tags"] == {
        "still": [1, 8],
        **{f"idle:{d}": [9 + i * 2, 10 + i * 2] for i, d in enumerate(GENERATE)},
    }
    for name in record["source"]:
        source = repair / "source" / name
        original = job / ("raw" if name.startswith("still_") else "processed/05_registered") / name
        assert source.read_bytes() == original.read_bytes()
        assert source.stat().st_mode & 0o777 == 0o444
    first = export(repair)
    assert diff(repair)["ok"]
    assert len(diff(repair)["frames"]) == 18
    for d in ("north-west", "west", "south-west"):
        n = ORDER.index(d) + 1
        assert np.array_equal(load_rgba(first / f"{n}.png"), load_rgba(job / "raw" / f"still_{d}.png"))
        assert not np.array_equal(
            load_rgba(first / f"{n}.png"), load_rgba(job / "raw" / f"still_{MIRROR[d]}.png")[:, ::-1]
        )
    snapshot = hashes(first)
    second = export(repair)
    assert second.name == "export-0002" and hashes(first) == snapshot
    assert diff(repair)["export"] == str(second) and diff(repair)["ok"]
    assert before == hashes(job)
    assert set(p.name for p in (first / "mapped").iterdir()) == set(record["source"])
    assert read_json(first / "export.json")["files"] == {k: v for k, v in snapshot.items() if k != "export.json"}


def test_failed_export_kept_and_never_masks_failure_with_earlier_snapshot(fixture, monkeypatch):
    repair = prepare(fixture)
    first = export(repair)
    before = hashes(first)
    converter = ws.FakeConverter()

    def failed_export(workspace, output):
        raise Refused("conversion failed")

    monkeypatch.setattr(converter, "export", failed_export)
    with pytest.raises(Refused, match="conversion failed"):
        ws.export(repair, converter=converter)
    assert hashes(first) == before and (repair / "workspace/export-0002").is_dir()
    with pytest.raises(Refused, match="incomplete"):
        diff(repair)
    assert export(repair).name == "export-0003" and diff(repair)["ok"]


def test_palette_png_reordered_and_hidden_rgb_equal(fixture):
    repair = prepare(fixture)
    output = export(repair)
    original = load_rgba(output / "1.png")
    indexed = Image.new("P", (16, 16), 8)
    colours = [*reversed(PALETTE), (98, 97, 96)]
    indexed.putpalette([v for rgb in colours for v in rgb] + [0] * (768 - 3 * len(colours)))
    indices = np.full((16, 16), 8, np.uint8)
    indices[original[..., 3] == 255] = 7
    indexed.putdata(indices.ravel())
    indexed.save(output / "1.png", transparency=8)
    assert diff(repair)["ok"]


@pytest.mark.parametrize("rgba,field", [((1, 2, 3, 255), "off_palette"), ((*PALETTE[0], 128), "invalid_alpha")])
def test_bad_palette_or_alpha_exact_coordinate(fixture, rgba, field):
    repair = prepare(fixture)
    output = export(repair)
    image = load_rgba(output / "1.png")
    image[7, 9] = rgba
    Image.fromarray(image).save(output / "1.png")
    result = diff(repair)
    assert not result["ok"]
    assert result["frames"]["1"]["changed"] == [[9, 7]]
    assert result["frames"]["1"][field] == [[9, 7]]


@pytest.mark.parametrize(
    "change",
    ["missing", "extra", "reorder", "duration", "tag", "canvas", "layer", "mode", "palette", "count", "mapping"],
)
def test_document_changes_detected(fixture, change):
    repair = prepare(fixture)
    output = export(repair)
    meta = read_json(output / "meta.json")
    if change == "missing":
        (output / "1.png").unlink()
    elif change == "extra":
        shutil.copyfile(output / "1.png", output / "19.png")
    elif change == "reorder":
        a, b = (output / "1.png").read_bytes(), (output / "2.png").read_bytes()
        (output / "1.png").write_bytes(b)
        (output / "2.png").write_bytes(a)
    elif change == "mapping":
        mapping = read_json(repair / "mapping.json")
        mapping["1"], mapping["2"] = mapping["2"], mapping["1"]
        write(repair / "mapping.json", mapping)
    else:
        if change == "duration":
            meta["durations_ms"][8] += 1
        elif change == "tag":
            meta["tags"]["idle:east"][1] -= 1
        elif change == "canvas":
            meta["width"] += 1
        elif change == "layer":
            meta["layers"].append("extra")
        elif change == "mode":
            meta["color_mode"] = "indexed"
        elif change == "palette":
            meta["palette"][1] = "#010203ff"
        elif change == "count":
            meta["frame_count"] -= 1
        write(output / "meta.json", meta)
    assert not diff(repair)["ok"]


@pytest.mark.parametrize("where", ["existing", "parent", "game", "git", "job", "symlink", "outside", "root"])
def test_refuse_output(fixture, where):
    job, profile, repair = fixture
    if where == "existing":
        repair.mkdir()
    elif where in ("parent", "game"):
        repair = (job if where == "parent" else profile.export.godot_project) / "repair"
    elif where in ("git", "job", "symlink"):
        container = repair.parent / "container"
        if where == "symlink":
            container.symlink_to(repair.parent / "missing", target_is_directory=True)
        else:
            container.mkdir()
            (container / (".git" if where == "git" else "manifest.json")).write_text(
                "gitdir: /elsewhere\n" if where == "git" else "{}"
            )
        repair = container / "new/repair"
    elif where == "outside":
        repair = job.parent / "../outside-repair"
    elif where == "root":
        repair = job.parent
    before = hashes(job.parent)
    paths = set(job.parent.rglob("*"))
    with pytest.raises(Refused, match=r"exists|inside|symlink|strictly"):
        ws.prepare(job, profile, "hero", "idle", repair, converter=ws.FakeConverter())
    assert hashes(job.parent) == before
    assert set(job.parent.rglob("*")) == paths
    if where != "existing" and where != "root":
        assert not repair.exists()


def test_prepare_refuses_palette_checkout(fixture):
    job, profile, repair = fixture
    checkout = job.parent / "palette-checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    palette = checkout / "art/palette.png"
    palette.parent.mkdir()
    shutil.copyfile(profile.palette.source, palette)
    profile = replace(profile, palette=replace(profile.palette, source=palette))
    repair = checkout / "somewhere/new/repair"
    before = hashes(job.parent)
    paths = set(job.parent.rglob("*"))
    with pytest.raises(Refused, match=r"^forbidden_path:"):
        ws.prepare(job, profile, "hero", "idle", repair, converter=ws.FakeConverter())
    assert hashes(job.parent) == before
    assert set(job.parent.rglob("*")) == paths
    assert not repair.exists()


def test_prepare_creates_missing_staging_root(fixture, monkeypatch):
    job, profile, _ = fixture
    root = job.parent / "missing/repairs"
    monkeypatch.setenv("SONORANLAB_REPAIR_ROOT", str(root))
    repair = root / "new"
    assert prepare((job, profile, repair)) == repair
    assert (repair / "repair.json").is_file()


@pytest.mark.parametrize(
    "relative", ["raw/still_west.png", "processed/05_registered", "report.json", "export/hero", "review.json"]
)
def test_refuse_symlinked_input(fixture, relative):
    job, profile, repair = fixture
    path = job / relative
    moved = job.parent / "moved"
    path.rename(moved)
    path.symlink_to(moved)
    with pytest.raises(Refused, match="symlink"):
        ws.prepare(job, profile, "hero", "idle", repair, converter=ws.FakeConverter())
    assert not repair.exists()


def test_fractional_milliseconds_refused(fixture):
    job, profile, repair = fixture
    row = profile.characters["hero"]
    row = replace(row, animations={"idle": replace(row.animations["idle"], fps=12)})
    profile = replace(profile, characters={"hero": row})
    with pytest.raises(Refused, match="integer"):
        ws.prepare(job, profile, "hero", "idle", repair, converter=ws.FakeConverter())


@pytest.mark.parametrize("field", ["result", "profile", "inputs", "outputs"])
def test_malformed_manifest_has_a_clean_error(fixture, field):
    job, _, repair = fixture
    manifest = read_json(job / "manifest.json")
    manifest[field] = None
    write(job / "manifest.json", manifest)
    with pytest.raises(ValueError, match="expected JSON object"):
        prepare(fixture)
    assert not repair.exists()


def test_source_hash_tampering_and_abort(fixture):
    repair = prepare(fixture)
    export(repair)
    source = repair / "source/still_west.png"
    source.chmod(0o644)
    source.write_bytes(source.read_bytes() + b"tamper")
    assert not diff(repair)["ok"]
    before = hashes(repair / "source")
    assert ws.abort(repair)["state"] == "aborted"
    assert ws.abort(repair)["state"] == "aborted"
    assert ws.inspect(repair)["state"] == "aborted"
    with pytest.raises(Refused, match="aborted"):
        export(repair)
    assert hashes(repair / "source") == before


def test_png_profile_gamma_recorded(fixture):
    repair = prepare(fixture)
    target = repair / "workspace/fake-frames/1.png"
    image = Image.open(target)
    chunks = PngImagePlugin.PngInfo()
    chunks.add(b"gAMA", (45455).to_bytes(4, "big"))
    chunks.add(b"sRGB", b"\x00")
    image.save(target, pnginfo=chunks)
    output = export(repair)
    actual = read_json(output / "meta.json")["png_chunks"]["1.png"]
    assert actual == png_chunks(target) and [c["type"] for c in actual] == ["gAMA", "sRGB"]
    assert diff(repair)["ok"]


def test_cli_json_and_exit_codes(fixture, monkeypatch, capsys):
    job, profile, repair = fixture
    monkeypatch.setattr(ws, "AsepriteConverter", ws.FakeConverter)
    assert (
        main(
            [
                "repair",
                "prepare",
                str(job),
                "--profile",
                str(profile.path),
                "--character",
                "hero",
                "--anim",
                "idle",
                "--out",
                str(repair),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["state"] == "prepared"
    for action in ("inspect", "export", "diff"):
        assert main(["repair", action, str(repair)]) == 0
        assert json.loads(capsys.readouterr().out)["ok"]
    assert main(["repair", "abort", str(repair)]) == 0
    capsys.readouterr()
    assert main(["repair", "export", str(repair)]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "refused"
    assert main(["repair", "inspect", str(repair / "missing")]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "io_or_usage_error"
    with pytest.raises(SystemExit) as error:
        main(["repair", "prepare"])
    assert error.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "usage_error"
    with pytest.raises(SystemExit) as error:
        main(["repair", "inspect", str(repair), "--unexpected"])
    assert error.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "usage_error"


def test_runtime_absent_and_core_does_not_import_repair(fixture, monkeypatch, capsys):
    monkeypatch.setenv("SONORANLAB_EDITOR_RUNTIME", "/nonexistent/sonoranlab-editor")
    assert "missing" in runtime.unavailable_reason()
    with pytest.raises(EditorUnavailable):
        runtime.AsepriteConverter()
    job, profile, repair = fixture
    assert (
        main(
            [
                "repair",
                "prepare",
                str(job),
                "--profile",
                str(profile.path),
                "--character",
                "hero",
                "--anim",
                "idle",
                "--out",
                str(repair),
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["error"] == "editor_unavailable" and not repair.exists()
    code = (
        "import sys; from sonoranlab import profile,pipeline,slice,validate,review,cli; "
        "cli.build_parser(); assert not any(k.startswith('sonoranlab.repair') for k in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], check=True, env=os.environ.copy())


def test_sandbox_argv_and_seed_home(tmp_path, monkeypatch):
    workspace, home = tmp_path / "workspace", tmp_path / "session/home"
    workspace.mkdir()
    runtime.seed_home(home)
    assert (home / ".config/aseprite/aseprite.ini").read_text() == (
        "[script_access]\n" + hashlib.sha1(runtime.EXTENSION_SCRIPT.encode()).hexdigest() + " = 31\n"
    )
    rt = runtime.Runtime(Path("/runtime"), Path("/runtime/aseprite"), Path("/runtime/artist"), Path("/runtime/node"))
    argv = runtime.bwrap_argv(rt, workspace, home)
    assert "--unshare-all" in argv and "--clearenv" in argv
    writable = [(argv[i + 1], argv[i + 2]) for i, arg in enumerate(argv) if arg == "--bind"]
    assert writable == [(str(home), "/home/sbx"), (str(workspace), "/work")]
    assert argv[argv.index("PATH") + 1] == "/usr/bin"
    assert not any(x in argv for x in ("ASEPRITE_AI_ALLOW_LUA", "ASEPRITE_AI_HEADLESS", "ASEPRITE_PATH"))
    with monkeypatch.context() as patch:
        x_socket = tmp_path / "X99"
        # Namespace-restricted CI can forbid even AF_UNIX bind; the builder only needs stat's socket type.
        patch.setattr(Path, "is_socket", lambda path: path == x_socket)
        gui = runtime.bwrap_argv(rt, workspace, home, mode="gui", x_socket=x_socket)
    assert gui[gui.index("DISPLAY") + 1] == ":99" and "/tmp/.X11-unix/X99" in gui
    assert "-b" not in gui
    with pytest.raises(Refused):
        runtime.bwrap_argv(rt, workspace, home, output="../source")
    with pytest.raises(Refused):
        runtime.bwrap_argv(rt, workspace, home, mode="gui")


@pytest.mark.aseprite
def test_real_batch_round_trip(fixture):
    if reason := runtime.unavailable_reason():
        pytest.skip(reason)
    job, _, _ = fixture
    before = hashes(job)
    converter = runtime.AsepriteConverter()
    repair = prepare(fixture, converter)
    output = Path(ws.export(repair, converter=converter)["export"])
    assert compare(repair, output)["ok"], compare(repair, output)
    assert hashes(job) == before
