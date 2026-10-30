"""DYL-133: compose per-clip jobs into one class sheet; build, apply, verify and roll back a delivery bundle in a
disposable synthetic game snapshot (a tmp git repo with a fake characters.json; no Godot, no private art)."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import yaml
from PIL import Image
from test_pipeline import MIRROR, ORDER, PALETTE, process, raw_set, stills_dir, toy_profile, tree_hashes

from sonoranlab import compose as compose_mod
from sonoranlab import deliver
from sonoranlab.cli import main
from sonoranlab.review import production_usable, record_review

PASS = {
    "verdict": "pass",
    "reviewer": "Owner",
    "cleanup_minutes": 0,
    "started_at": "2026-10-28T10:00:00+00:00",
    "ended_at": "2026-10-28T10:05:00+00:00",
}
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run([*GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


IMPORT = f"""[remap]

importer="texture"
type="CompressedTexture2D"
uid="uid://bknight0sib1"
path="res://.godot/imported/knight.png-{md5("res://sprites/knight.png")}.ctex"
metadata={{
"vram_texture": false
}}

[deps]

source_file="res://sprites/knight.png"
dest_files=["res://.godot/imported/knight.png-{md5("res://sprites/knight.png")}.ctex"]

[params]

compress/mode=0
mipmaps/generate=false
process/fix_alpha_border=true
detect_3d/compress_to=1
"""


def base_manifest(extra: dict | None = None) -> bytes:
    knight = {"sheet": "knight.png", "cell": 24, "columns": 3, "standing_pixels": 10, "height_m": 0.161}
    knight |= {"clips": [{"name": "idle", "start": 1, "frames": 2, "fps": 5, "loop": True}], "colours": 3}
    doc = {"density_px_per_m": 62, "rows": ORDER, "palette": "palette.png", "mirrored_animation_rows": MIRROR}
    doc["characters"] = {"knight": knight, **(extra or {})}
    return (json.dumps(doc, indent=1) + "\n").encode()


# A full class's clips as the game's table has them (loop false: no hold field), at toy frame counts.
ANIMS = {
    "run": {"frames": 2, "fps": 10, "loop": True},
    "cast": {"frames": 2, "fps": 10, "loop": False, "track_default": "E"},
    "death": {"frames": 2, "fps": 8, "loop": False, "hold_last": True, "track_default": "E", "validator_mode": "fall"},
}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """Two real passing clip jobs (pipeline.run on the toy character, slow: built once), the stills, a synthetic game
    repo at one revision and a class request."""
    root = tmp_path_factory.mktemp("world")
    prof = toy_profile(root, lambda d: d["animations"].update(ANIMS))
    raw, stills = raw_set(root), stills_dir(root)
    jobs = {}
    for anim in ("idle", "run", "cast", "death"):
        assert process(raw, prof, root / anim, "--stills", str(stills), anim=anim) == 0
        jobs[anim] = root / anim
    game = root / "game-src"
    (game / "sprites").mkdir(parents=True)
    (game / "sprites" / "characters.json").write_bytes(base_manifest())
    Image.fromarray(np.zeros((8 * 24, 72, 4), np.uint8)).save(game / "sprites" / "knight.png")
    (game / "sprites" / "knight.png.import").write_text(IMPORT)
    (game / "README").write_text("synthetic game\n")
    git(game, "init", "-q")
    git(game, "add", "-A")
    git(game, "commit", "-q", "-m", "base")
    rev = git(game, "rev-parse", "HEAD").strip()
    request = root / "request.yaml"
    request.write_text(
        yaml.safe_dump(
            {"project": "toy", "character": "hero", "display_name": "Hero", "height_m": 0.097, "clips": ["run", "idle"]}
        )
    )
    return {"root": root, "profile": prof, "jobs": jobs, "stills": stills, "game": game, "rev": rev, "request": request}


def run_compose(world, out: Path, stills: Path | None = None, names=("idle", "run")) -> int:
    clips = ",".join(f"{n}={world['jobs'][n]}" for n in names)
    argv = ["compose", "--profile", str(world["profile"]), "--character", "hero", "--clips", clips]
    return main([*argv, "--stills", str(stills or world["stills"]), "--out", str(out)])


@pytest.fixture
def approved(world, tmp_path):
    job = tmp_path / "composed"
    assert run_compose(world, job) == 0
    record_review(job, **PASS)
    assert production_usable(job)
    return job


def build_args(world, composed: Path, out: Path, **over) -> list[str]:
    opts = {
        "--profile": world["profile"],
        "--composed": composed,
        "--request": world["request"],
        "--target-rev": world["rev"],
        "--base-manifest": world["game"] / "sprites" / "characters.json",
        "--import-template": world["game"] / "sprites" / "knight.png.import",
        "--out": out,
    } | over
    return ["deliver", "build", *(str(x) for kv in opts.items() for x in kv)]


@pytest.fixture
def bundle(world, approved, tmp_path):
    out = tmp_path / "bundle"
    assert main(build_args(world, approved, out)) == 0
    return out


@pytest.fixture
def snap(world, tmp_path):
    dst = tmp_path / "snapshot"
    subprocess.run(["git", "clone", "-q", str(world["game"]), str(dst)], check=True)
    return dst


def files_of(root: Path) -> dict[str, str]:
    return {k: v for k, v in tree_hashes(root).items() if not k.startswith(".git/")}


def bundle_doc(bundle: Path) -> dict:
    return json.loads((bundle / "bundle.json").read_text())


# ---------------------------------------------------------------- compose


def test_compose_happy_path(world, tmp_path, capsys):
    inputs = {n: tree_hashes(j) for n, j in world["jobs"].items()} | {"stills": tree_hashes(world["stills"])}
    job = tmp_path / "composed"
    assert run_compose(world, job) == 0, capsys.readouterr().err
    assert {n: tree_hashes(j) for n, j in world["jobs"].items()} | {"stills": tree_hashes(world["stills"])} == inputs
    sheet = np.asarray(Image.open(job / "export" / "hero" / "hero.png").convert("RGBA"))
    assert sheet.shape == (8 * 24, 5 * 24, 4)
    for r, d in enumerate(ORDER):
        still = np.asarray(Image.open(world["stills"] / f"still_{d}.png").convert("RGBA")).copy()
        still[still[..., 3] == 0] = 0
        assert np.array_equal(sheet[r * 24 : (r + 1) * 24, :24], still)
    src = {
        n: np.asarray(Image.open(j / "export" / "hero" / "hero.png").convert("RGBA")) for n, j in world["jobs"].items()
    }
    assert np.array_equal(sheet[:, 24:72], src["idle"][:, 24:]) and np.array_equal(sheet[:, 72:], src["run"][:, 24:])
    man = json.loads((job / "manifest.json").read_text())
    assert [(c["name"], c["start"]) for c in man["params"]["clips"]] == [("idle", 1), ("run", 3)]
    assert man["result"] == {"pass": True, "exit": 0, "exported": True, "error": None}
    report = json.loads((job / "report.json").read_text())
    assert report["pass"] and set(report["clips"]) == {"idle", "run"}
    assert set(report["provenance"]["sources"]) == {"idle", "run"}
    side = json.loads((job / "export" / "hero" / "hero.sonoranlab.json").read_text())
    assert side["characters_json_entry"]["columns"] == 5
    assert not production_usable(job)
    record_review(job, **PASS)  # the owner's 1x review binds to the composed sheet
    assert production_usable(job)
    assert run_compose(world, job) == 2  # exclusive


def test_compose_and_build_a_four_clip_class(world, tmp_path, capsys):
    """idle, run, cast, death: start columns follow the clips, west rows mirror east, the clip table carries no hold
    field (a non-loop clip holds its last frame in the game) and a request listing all four is delivered complete."""
    job = tmp_path / "composed"
    assert run_compose(world, job, names=("idle", "run", "cast", "death")) == 0, capsys.readouterr().err
    side = json.loads((job / "export" / "hero" / "hero.sonoranlab.json").read_text())
    entry = side["characters_json_entry"]
    assert entry["columns"] == 9 and entry["clips"] == [
        {"name": "idle", "start": 1, "frames": 2, "fps": 5, "loop": True},
        {"name": "run", "start": 3, "frames": 2, "fps": 10, "loop": True},
        {"name": "cast", "start": 5, "frames": 2, "fps": 10, "loop": False},
        {"name": "death", "start": 7, "frames": 2, "fps": 8, "loop": False},
    ]
    report = json.loads((job / "report.json").read_text())
    assert report["pass"] and report["clips"]["death"]["validator_mode"] == "fall"
    assert "validator_mode" not in report["clips"]["cast"]
    sheet = np.asarray(Image.open(job / "export" / "hero" / "hero.png").convert("RGBA"))
    assert sheet.shape == (8 * 24, 9 * 24, 4)
    for west, east in MIRROR.items():
        w, e = (sheet[ORDER.index(r) * 24 : (ORDER.index(r) + 1) * 24, 24:] for r in (west, east))
        for c in range(8):
            assert np.array_equal(w[:, c * 24 : (c + 1) * 24], e[:, c * 24 : (c + 1) * 24][:, ::-1]), (west, c)
    record_review(job, **PASS)
    request = tmp_path / "request.yaml"
    doc = yaml.safe_load(world["request"].read_text()) | {"clips": ["run", "idle", "cast", "death"]}
    request.write_text(yaml.safe_dump(doc))
    assert main(build_args(world, job, tmp_path / "bundle", **{"--request": request})) == 0
    bundle = bundle_doc(tmp_path / "bundle")
    assert bundle["known_missing_clips"] == []
    hero = json.loads((tmp_path / "bundle" / "files" / "sprites" / "characters.json").read_bytes())["characters"][
        "hero"
    ]
    assert hero["columns"] == 9 and [list(c) for c in hero["clips"]] == [["name", "start", "frames", "fps", "loop"]] * 4
    # a request listing a clip the sheet lacks is refused
    assert main(build_args(world, job, tmp_path / "b2", **{"--request": world["request"]})) == 0
    two = tmp_path / "two"
    assert run_compose(world, two) == 0
    record_review(two, **PASS)
    assert main(build_args(world, two, tmp_path / "b3", **{"--request": request})) == 2
    assert "requires clips ['cast', 'death']" in capsys.readouterr().err


def test_compose_refuses_stills_that_differ_from_the_jobs(world, tmp_path, capsys):
    stills = shutil.copytree(world["stills"], tmp_path / "stills")
    p = stills / "still_west.png"
    a = np.asarray(Image.open(p).convert("RGBA")).copy()
    a[5, 11] = (*PALETTE[3], 255)
    Image.fromarray(a).save(p)
    assert run_compose(world, tmp_path / "out", stills) == 2
    assert "not pixel-identical" in capsys.readouterr().err and not (tmp_path / "out").exists()


def test_compose_refuses_a_job_for_another_clip(world, tmp_path, capsys):
    argv = ["compose", "--profile", str(world["profile"]), "--character", "hero", "--stills", str(world["stills"])]
    assert main([*argv, "--clips", f"run={world['jobs']['idle']}", "--out", str(tmp_path / "o")]) == 2
    assert "expected hero/run" in capsys.readouterr().err and not (tmp_path / "o").exists()


def test_compose_validator_failure_exits_1_without_export(world, tmp_path, monkeypatch):
    real = compose_mod.validate

    def failing(*a, **k):
        rep = real(*a, **k)
        rep["failures"].append("synthetic failure")
        return rep

    monkeypatch.setattr(compose_mod, "validate", failing)
    job = tmp_path / "composed"
    assert run_compose(world, job) == 1
    assert not (job / "export").exists()
    assert json.loads((job / "manifest.json").read_text())["result"]["exported"] is False


# ---------------------------------------------------------------- build


def test_build_bundle(world, bundle, approved):
    doc = bundle_doc(bundle)
    assert doc["schema"] == "sonoranlab.delivery_bundle/1" and doc["state"] == "BUILT"
    assert doc["target"] == {
        "game": "TwosAreUp",
        "revision": world["rev"],
        "base_manifest_sha256": hashlib.sha256(base_manifest()).hexdigest(),
    }
    assert [(f["path"], f["action"]) for f in doc["files"]] == [
        ("sprites/hero.png", "add"),
        ("sprites/hero.png.import", "add"),
        ("sprites/characters.json", "modify"),
    ]
    assert {k: v["value"] for k, v in doc["states"].items()} == {
        "ARTIFACT_APPROVED": True,
        "STAGED_AND_ENGINE_VERIFIED": False,
        "READY_TO_INSTALL": False,
        "INSTALLED": False,
    }
    assert [m["name"] for m in doc["known_missing_clips"]] == ["cast", "death"] and doc["playable_class"] is False
    patched = (bundle / "files" / "sprites" / "characters.json").read_bytes()
    new, old = json.loads(patched), json.loads(base_manifest())
    hero = new["characters"].pop("hero")
    assert new == old and list(new["characters"]) == ["knight"]  # unrelated entries and order preserved
    assert list(hero) == ["sheet", "cell", "columns", "standing_pixels", "height_m", "clips", "colours"]
    assert hero["standing_pixels"] == 6 == round(0.097 * 62) and hero["height_m"] == 0.097
    assert [c["start"] for c in hero["clips"]] == [1, 3] and hero["columns"] == 5
    assert patched.startswith(base_manifest()[:-6])  # appended, nothing above it rewritten
    assert list(hero["clips"][0]) == ["name", "start", "frames", "fps", "loop"]  # the game's key order
    assert '+  "hero": {' in (bundle / "characters.json.diff").read_text()
    imp = (bundle / "files" / "sprites" / "hero.png.import").read_text()
    assert 'source_file="res://sprites/hero.png"' in imp and f"hero.png-{md5('res://sprites/hero.png')}.ctex" in imp
    assert "knight" not in imp and 'uid="uid://' in imp and "compress/mode=0" in imp
    sheet = bundle / "files" / "sprites" / "hero.png"
    assert sheet.read_bytes() == (approved / "export" / "hero" / "hero.png").read_bytes()
    install = (bundle / "INSTALL.md").read_text()
    assert world["rev"] in install and "known missing clips" in install and "deliver apply` refuses" in install
    after = (bundle / "checks" / "after.sha256").read_text().splitlines()
    assert after == [f"{f['sha256_after']}  {f['path']}" for f in doc["files"]]
    assert "Owner" in (bundle / "provenance.md").read_text()


def test_build_refuses_without_owner_review(world, tmp_path, capsys):
    job = tmp_path / "composed"
    assert run_compose(world, job) == 0
    assert main(build_args(world, job, tmp_path / "b")) == 2
    assert "owner 1x review required" in capsys.readouterr().err and not (tmp_path / "b").exists()


@pytest.mark.parametrize("tamper", ["verdict", "sheet"])
def test_build_refuses_a_modified_review(world, approved, tmp_path, capsys, tamper):
    if tamper == "verdict":
        p = approved / "review.json"
        p.write_text(p.read_text().replace('"verdict": "pass"', '"verdict": "fail"'))
    else:
        p = approved / "export" / "hero" / "hero.png"
        p.write_bytes(p.read_bytes() + b"\0")
    assert main(build_args(world, approved, tmp_path / "b")) == 2
    assert "not production usable" in capsys.readouterr().err and not (tmp_path / "b").exists()


def test_build_refuses_an_existing_id(world, approved, tmp_path, capsys):
    base = tmp_path / "characters.json"
    base.write_bytes(base_manifest({"hero": {"sheet": "hero.png"}}))
    assert main(build_args(world, approved, tmp_path / "b", **{"--base-manifest": base})) == 2
    assert "already exists in the base manifest" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("over", "msg"),
    [
        ({"--target-rev": "HEAD"}, "full 40-hex"),
        ({"--request": "height"}, "south still stands 6 px"),
        ({"--import-template": "mipmaps"}, "pixel-art setting"),
    ],
)
def test_build_refuses_bad_inputs(world, approved, tmp_path, capsys, over, msg):
    if over.get("--request") == "height":
        req = tmp_path / "req.yaml"
        req.write_text(yaml.safe_dump({"project": "toy", "character": "hero", "height_m": 1.8}))
        over = {"--request": req}
    if over.get("--import-template") == "mipmaps":
        imp = tmp_path / "x.png.import"
        imp.write_text(IMPORT.replace("mipmaps/generate=false", "mipmaps/generate=true"))
        over = {"--import-template": imp}
    assert main(build_args(world, approved, tmp_path / "b", **over)) == 2
    assert msg in capsys.readouterr().err and not (tmp_path / "b").exists()


def test_godot_uid_text_form():
    uid = deliver.godot_uid("x")
    assert uid.startswith("uid://") and set(uid[6:]) <= set("abcdefghijklmnopqrstuvwxy012345678")
    alphabet = "abcdefghijklmnopqrstuvwxy012345678"
    seen = "".join(deliver.godot_uid(str(i))[6:] for i in range(300))
    assert set(seen) == set(alphabet)  # every digit is used, and never `z` or `9` (Godot rewrites those)
    # gun's id/sheet seed: the old a-z/0-7 encoder emitted `bdoggxzlhctho`, which Godot 4.7.2 rewrote to this
    seed = "gun:914dabb93b36fcc9cbc85c72ac02700811384276d1e27d26fd89b190cffefb30"
    assert deliver.godot_uid(seed) == "uid://bdoggx0lhctho"
    assert uid == deliver.godot_uid("x") != deliver.godot_uid("y")


# ---------------------------------------------------------------- apply / verify / rollback


def test_apply_verify_rollback_round_trip(world, bundle, snap, tmp_path):
    before = files_of(snap)
    res = deliver.apply(bundle, snap)
    doc = bundle_doc(bundle)
    for f in doc["files"]:
        assert hashlib.sha256((snap / f["path"]).read_bytes()).hexdigest() == f["sha256_after"]
    assert bundle_doc(bundle)["states"]["INSTALLED"]["value"] is False  # apply never installs
    journal = json.loads(Path(res["journal"]).read_text())
    assert journal["status"] == "applied" and "sprites/characters.json" in journal["backups"]
    # files only: verified but not engine-verified
    v = deliver.verify(bundle, snap)
    assert v["ok"] and v["files_ok"] and not v["engine_verified"] and bundle_doc(bundle)["state"] == "BUILT"
    # with a passing game-consumer result for exactly these files
    result = tmp_path / "result.json"
    result.write_text(
        json.dumps(
            {
                "schema": deliver.CONSUMER_SCHEMA,
                "pass": True,
                "character": "hero",
                "target_head": world["rev"],
                "files_sha256": {f["path"]: f["sha256_after"] for f in doc["files"]},
            }
        )
    )
    v = deliver.verify(bundle, snap, result)
    doc = bundle_doc(bundle)
    assert v["ok"] and doc["state"] == "READY_TO_INSTALL"
    assert doc["states"]["STAGED_AND_ENGINE_VERIFIED"]["value"] and doc["states"]["READY_TO_INSTALL"]["value"]
    assert doc["states"]["INSTALLED"] == {
        "value": False,
        "evidence": None,
        "set_by": "the owner's install record only (INSTALL.md)",
    }
    deliver.load_bundle(bundle)  # state updates do not break bundle integrity
    with pytest.raises(deliver.DeliveryError, match="already applied"):
        deliver.apply(bundle, snap)
    r = deliver.rollback(bundle, snap)
    assert r["ok"] and r["target_status_after"] == [] and files_of(snap) == before
    assert git(snap, "status", "--porcelain", "--untracked-files=all") == ""
    deliver.apply(bundle, snap)  # a repeat apply after rollback works
    assert deliver.rollback(bundle, snap)["ok"] and files_of(snap) == before
    assert sorted(p.name.split(".")[1] for p in (bundle / ".applied").iterdir()) == ["rolledback-1", "rolledback-2"]


def test_verify_rejects_a_consumer_result_for_other_files(world, bundle, snap, tmp_path):
    deliver.apply(bundle, snap)
    result = tmp_path / "result.json"
    doc = bundle_doc(bundle)
    fields = {"schema": deliver.CONSUMER_SCHEMA, "pass": True, "character": "hero", "target_head": world["rev"]}
    result.write_text(json.dumps(fields | {"files_sha256": {f["path"]: "0" * 64 for f in doc["files"]}}))
    v = deliver.verify(bundle, snap, result)
    assert not v["ok"] and v["files_ok"] and bundle_doc(bundle)["state"] == "BUILT"
    assert main(["deliver", "verify", str(bundle), "--target", str(snap), "--consumer-result", str(result)]) == 1


def test_apply_refuses_the_protected_game_checkout(world, bundle, tmp_path, monkeypatch):
    game = tmp_path / "protected"
    subprocess.run(["git", "clone", "-q", str(world["game"]), str(game)], check=True)
    from sonoranlab.api import paths

    monkeypatch.setattr(paths, "_game_roots", lambda: (game.resolve(),))  # the profiles' export.godot_project
    before = files_of(game)
    link = tmp_path / "innocent"
    link.symlink_to(game)
    for target in (game, link, game / "sprites"):
        with pytest.raises(deliver.DeliveryError, match="protected game checkout"):
            deliver.apply(bundle, target)
    assert files_of(game) == before and not (bundle / ".applied").exists()
    # the profile's own godot_project is protected too
    own = Path(world["root"]) / "game"
    with pytest.raises(deliver.DeliveryError, match="protected game checkout"):
        deliver.apply(bundle, own)


def test_apply_refuses_dirty_wrong_head_and_stale_base(world, bundle, snap, tmp_path):
    (snap / "notes.txt").write_text("dirty")
    with pytest.raises(deliver.DeliveryError, match="dirty"):
        deliver.apply(bundle, snap)
    (snap / "notes.txt").unlink()
    (snap / "README").write_text("moved on\n")
    git(snap, "commit", "-qam", "next")
    with pytest.raises(deliver.DeliveryError, match="not the bundle's target revision"):
        deliver.apply(bundle, snap)
    # stale base: the bundle patched a characters.json that is not the one at the target revision
    stale = tmp_path / "stale.json"
    stale.write_bytes(base_manifest({"other": {"sheet": "other.png"}}))
    out = tmp_path / "stale-bundle"
    approved = Path(bundle_doc(bundle)["source"]["composed_job"])
    assert main(build_args(world, approved, out, **{"--base-manifest": stale})) == 0
    fresh = tmp_path / "fresh"
    subprocess.run(["git", "clone", "-q", str(world["game"]), str(fresh)], check=True)
    with pytest.raises(deliver.DeliveryError, match="stale base"):
        deliver.apply(out, fresh)
    assert git(fresh, "status", "--porcelain", "--untracked-files=all") == ""


def test_apply_refuses_path_escape_and_symlinks(world, bundle, snap, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    (snap / ".git" / "info" / "exclude").write_text("sprites/hero.png\n")
    (snap / "sprites" / "hero.png").symlink_to(outside)  # ignored, so the tree is clean
    with pytest.raises(deliver.DeliveryError, match="symlink"):
        deliver.apply(bundle, snap)
    assert outside.read_bytes() == b"x"
    (snap / "sprites" / "hero.png").unlink()
    doc = bundle_doc(bundle)
    doc["files"][0]["path"] = "sprites/../../escape.png"
    (bundle / "bundle.json").write_text(json.dumps(doc))
    with pytest.raises(deliver.DeliveryError, match="plain relative path"):
        deliver.apply(bundle, snap)
    assert not (tmp_path / "escape.png").exists()


@pytest.mark.parametrize("tamper", ["asset", "review"])
def test_apply_refuses_a_modified_asset_or_review(world, bundle, snap, tamper):
    if tamper == "asset":
        p = bundle / "files" / "sprites" / "hero.png"
        p.write_bytes(p.read_bytes() + b"\0")
    else:
        p = Path(bundle_doc(bundle)["source"]["composed_job"]) / "review.json"
        p.write_text(p.read_text().replace('"verdict": "pass"', '"verdict": "fail"'))
    with pytest.raises(deliver.DeliveryError, match=r"modified after build|no longer binds"):
        deliver.apply(bundle, snap)
    assert git(snap, "status", "--porcelain", "--untracked-files=all") == ""


def test_partial_copy_failure_rolls_back_automatically(world, bundle, snap, monkeypatch):
    before = files_of(snap)
    real, calls = deliver._install_file, []

    def flaky(data, dest, mode):
        calls.append(dest)
        if len(calls) == 3:
            raise OSError("disk full (synthetic)")
        real(data, dest, mode)

    monkeypatch.setattr(deliver, "_install_file", flaky)
    with pytest.raises(deliver.DeliveryError, match="rolled back"):
        deliver.apply(bundle, snap)
    assert files_of(snap) == before and git(snap, "status", "--porcelain", "--untracked-files=all") == ""
    failed = [p for p in (bundle / ".applied").iterdir()]
    assert len(failed) == 1 and ".failed-1" in failed[0].name
    assert json.loads((failed[0] / "journal.json").read_text())["status"] == "failed_rolled_back"
    monkeypatch.setattr(deliver, "_install_file", real)
    deliver.apply(bundle, snap)  # not blocked by the failed attempt
    assert deliver.rollback(bundle, snap)["ok"]


def test_rollback_restores_bytes_changed_after_apply(world, bundle, snap):
    before = files_of(snap)
    deliver.apply(bundle, snap)
    (snap / "sprites" / "hero.png.import").write_text("rewritten by an editor\n")
    r = deliver.rollback(bundle, snap)
    assert r["ok"] and r["changed_since_apply"] == ["sprites/hero.png.import"] and files_of(snap) == before


def test_cli_round_trip(world, bundle, snap, capsys):
    assert main(["deliver", "apply", str(bundle), "--target", str(snap)]) == 0
    assert main(["deliver", "apply", str(bundle), "--target", str(snap)]) == 2
    assert main(["deliver", "verify", str(bundle), "--target", str(snap)]) == 0
    assert main(["deliver", "rollback", str(bundle), "--target", str(snap)]) == 0
    assert main(["deliver", "rollback", str(bundle), "--target", str(snap)]) == 2
    assert os.listdir(bundle / ".applied")


# ---------------------------------------------------------------- tools/game_consumer contract (no Godot, no dotnet)

CONSUMER = Path(__file__).resolve().parents[3] / "tools" / "game_consumer"


def fake_consumer_run(out: Path, *, tests_executed: int = 3, pixel_check: str = "known", gallery: bool = True) -> None:
    """What run.sh's supervised steps leave behind, without running the game: statuses, logs, a .trx, gallery PNGs."""
    logs = out / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for step in ("dotnet_test", "game_build", "godot_import", "pixel_check", "gallery_capture"):
        (logs / f"{step}.status").write_text(json.dumps({"exit": 0, "timed_out": False, "flooded": False}))
    if pixel_check != "pass":
        (logs / "pixel_check.status").write_text(json.dumps({"exit": 1, "timed_out": False, "flooded": False}))
        who = "hero" if pixel_check == "known" else "knight"
        (logs / "pixel_check.head.log").write_text(f"ERROR: System.InvalidOperationException: {who}: clip table\n")
    if gallery:
        (logs / "gallery_capture.tail.log").write_text("V0 Phase 2 gallery captures complete (20 standees): x\n")
        (out / "gallery").mkdir(exist_ok=True)
        (out / "gallery" / "gallery-overview.png").write_bytes(b"png")
    (out / "dotnet").mkdir(exist_ok=True)
    (out / "dotnet" / "tests.trx").write_text(
        '<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010"><ResultSummary outcome="Completed">'
        f'<Counters total="{tests_executed}" executed="{tests_executed}" passed="{tests_executed}" failed="0"/>'
        "</ResultSummary></TestRun>"
    )


def summarize(mode: str, bundle: Path, snap: Path, out: Path) -> subprocess.CompletedProcess:
    cmd = ["python3", "-I", str(CONSUMER / "summarize.py"), mode, str(bundle), str(snap), str(out)]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def test_consumer_result_feeds_verify(world, bundle, snap, tmp_path):
    deliver.apply(bundle, snap)
    out = tmp_path / "consumer"
    out.mkdir()
    assert summarize("pre", bundle, snap, out).returncode == 0
    fake_consumer_run(out)
    run = summarize("final", bundle, snap, out)
    assert run.returncode == 0, run.stdout + run.stderr
    result = json.loads((out / "result.json").read_text())
    assert result["game_pixel_check"]["verdict"] == "known_missing_clip"  # reported, not hidden
    assert result["fallback_render_non_consumer"] is None
    v = deliver.verify(bundle, snap, out / "result.json")
    assert v["ok"] and bundle_doc(bundle)["state"] == "READY_TO_INSTALL"


@pytest.mark.parametrize("case", ["no_tests", "pixel_check_other", "no_gallery", "engine_rewrite"])
def test_consumer_result_fails_honestly(world, bundle, snap, tmp_path, case):
    deliver.apply(bundle, snap)
    out = tmp_path / "consumer"
    out.mkdir()
    assert summarize("pre", bundle, snap, out).returncode == 0
    fake_consumer_run(
        out,
        tests_executed=0 if case == "no_tests" else 3,
        pixel_check="other" if case == "pixel_check_other" else "known",
        gallery=case != "no_gallery",
    )
    if case == "engine_rewrite":
        (snap / "sprites" / "hero.png.import").write_text("uid regenerated\n")
    assert summarize("final", bundle, snap, out).returncode == 1
    result = json.loads((out / "result.json").read_text())
    assert result["pass"] is False and result["failures"]
    v = deliver.verify(bundle, snap, out / "result.json")
    assert not v["ok"] and bundle_doc(bundle)["state"] == "BUILT"


def test_run_sh_refuses_before_running_anything(world, bundle, snap, tmp_path):
    def run(*args: str, **env: str) -> subprocess.CompletedProcess:
        e = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], **env}
        return subprocess.run(["bash", str(CONSUMER / "run.sh"), *args], capture_output=True, text=True, env=e)

    base = ["--snapshot", str(snap), "--bundle", str(bundle), "--out", str(tmp_path / "o")]
    r = run(*base)
    assert r.returncode == 2 and "zero selected tests" in r.stderr
    r = run(*base, "--test-class", "X", "--fallback-render", "--sibling", "a")
    assert r.returncode == 2 and "two --sibling" in r.stderr
    r = run(*base, "--test-class", "X", "--sibling", "a", "--sibling", "b", SONORANLAB_PROTECTED=str(snap))
    assert r.returncode == 2 and "protected game checkout" in r.stderr
    r = run(*base[:-1], str(snap / "o"), "--test-class", "X", "--sibling", "a", "--sibling", "b")
    assert r.returncode == 2 and "outside the snapshot" in r.stderr
    assert not (tmp_path / "o").exists() and not (snap / "sonoranlab_consumer_check").exists()
