"""Registry persistence, owner evidence, hostile paths and current artifact review."""

import importlib
import json
import os
import shutil
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml
from PIL import Image
from test_pipeline import CELL, ORDER, toy_profile
from test_review import PASS, exported, template  # noqa: F401 -- reuse the real pipeline fixtures
from test_review import job as real_job

from sonoranlab import registry as records
from sonoranlab.api import Context, ToolError, call, paths
from sonoranlab.artifacts import sha256_file
from sonoranlab.cli import main
from sonoranlab.review import record_review

job = real_job


@pytest.fixture
def setup(tmp_path, monkeypatch):
    profile = toy_profile(tmp_path)
    repo = tmp_path / "repo"
    (repo / "profiles").mkdir(parents=True)
    data = yaml.safe_load(profile.read_text())
    data["export"]["godot_project"] = str(tmp_path / "game")
    data["palette"]["source"] = str(tmp_path / "palette.png")
    (repo / "profiles/toy.yaml").write_text(yaml.safe_dump(data))
    ctx = Context(repo)
    monkeypatch.setattr(paths, "Context", lambda: ctx)
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", str(tmp_path))
    monkeypatch.setenv("SONORANLAB_STATE", str(tmp_path / "state"))
    refs = tmp_path / "references"
    refs.mkdir()
    for direction in ORDER:
        Image.new("RGBA", (CELL, CELL), (1, 2, 3, 255)).save(refs / f"still_{direction}.png")
    return ctx, refs, tmp_path / "state"


def register(setup, **kwargs):
    ctx, refs, _ = setup
    return records.register_character("toy", "hero", str(refs), ctx=ctx, **kwargs)


def snapshot(root):
    return {
        p.relative_to(root).as_posix(): (p.stat().st_mtime_ns, sha256_file(p)) for p in root.rglob("*") if p.is_file()
    }


def test_roundtrip_idempotency_immutable_inputs_and_modes(setup):
    ctx, refs, state = setup
    before = snapshot(refs)
    (refs / "extra.png").write_bytes(b"not a still")
    first = register(setup, notes="owner supplied")
    second = register(setup)
    assert first["ref_set_id"] == second["ref_set_id"] and not second["created"]
    assert first["approval"]["state"] == "unapproved"
    assert {k: v for k, v in snapshot(refs).items() if k != "extra.png"} == before
    dest = state / "registry/toy/hero/refs" / first["ref_set_id"]
    assert stat.S_IMODE(dest.stat().st_mode) == 0o555
    assert len(list(dest.iterdir())) == 8
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o444 for p in dest.iterdir())
    loaded = records.get_character("toy", "hero", ctx=Context(ctx.repo))
    assert loaded["reference_sets"][0]["files"][ORDER[0]]["sha256"] == before[f"still_{ORDER[0]}.png"][1]
    assert loaded["registered"] and loaded["cell"] == [CELL, CELL]
    assert loaded["reference_sets"][0]["notes"] == "owner supplied"
    Image.new("RGBA", (CELL, CELL), (9, 2, 3, 255)).save(refs / f"still_{ORDER[0]}.png")
    third = register(setup)
    assert third["ref_set_id"] != first["ref_set_id"] and snapshot(dest)
    assert len(records.get_character("toy", "hero", ctx=ctx)["reference_sets"]) == 2


def test_approval_bound_to_hashes_and_append_only(setup):
    ctx, _, state = setup
    ref = register(setup)["ref_set_id"]
    a = records.approve_references("toy", "hero", ref, "Owner", "inspected eight stills", ctx=ctx)
    assert a["schema"] == "sonoranlab.ref-approval/1"
    records.approve_references("toy", "hero", ref, "Owner", "confirmed", ctx=ctx)
    assert len(list((state / "registry/toy/hero/approvals").glob("*.json"))) == 2
    assert records.get_character("toy", "hero", ctx=ctx)["reference_sets"][0]["approval"]["state"] == "approved"
    target = state / "registry/toy/hero/refs" / ref / f"still_{ORDER[0]}.png"
    target.chmod(0o644)
    target.write_bytes(b"tampered")
    assert records.get_character("toy", "hero", ctx=ctx)["reference_sets"][0]["approval"]["state"] == "invalid"
    with pytest.raises(ToolError, match="refusing overwrite"):
        register(setup)
    with pytest.raises(ToolError, match="no longer match"):
        records.approve_references("toy", "hero", ref, "Owner", "again", ctx=ctx)


def test_content_id_collision_is_refused_under_concurrent_registration(setup, monkeypatch):
    ctx, refs, _ = setup
    other = refs.parent / "other"
    shutil.copytree(refs, other)
    Image.new("RGBA", (CELL, CELL), (9, 2, 3, 255)).save(other / f"still_{ORDER[0]}.png")
    monkeypatch.setattr(records, "_ref_id", lambda files: "refs-" + "0" * 12)

    def attempt(source):
        try:
            return records.register_character("toy", "hero", str(source), ctx=ctx)
        except ToolError as exc:
            return exc.error

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attempt, [refs, other]))
    assert sum(r.get("created", False) for r in results) == 1
    assert [r["code"] for r in results if "code" in r] == ["conflict"]
    assert len(records.get_character("toy", "hero", ctx=ctx)["reference_sets"]) == 1


@pytest.mark.parametrize("kind", ["traversal", "outside", "symlink", "fifo", "device", "game"])
def test_hostile_reference_paths(setup, tmp_path, kind):
    ctx, refs, _ = setup
    target = refs / f"still_{ORDER[0]}.png"
    if kind == "traversal":
        source = refs / ".." / refs.name
    elif kind == "outside":
        source = "/nonexistent/outside"
    elif kind == "game":
        source = tmp_path / "game"
        shutil.copytree(refs, source, dirs_exist_ok=True)
    else:
        source = refs
        target.unlink()
        if kind == "fifo":
            os.mkfifo(target)
        else:
            target.symlink_to("/dev/null" if kind == "device" else "/etc/passwd")
    result = call("register_character", {"project": "toy", "character": "hero", "reference_dir": str(source)}, ctx=ctx)
    assert result["error"]["code"] == "path_refused", result
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("mode,size", [("RGB", CELL), ("RGBA", CELL + 1)])
def test_invalid_stills(setup, mode, size):
    _, refs, _ = setup
    Image.new(mode, (size, size)).save(refs / f"still_{ORDER[0]}.png")
    with pytest.raises(ToolError, match="alpha channel"):
        register(setup)


@pytest.mark.parametrize("bad", ["{", "[]", '{"schema":"sonoranlab.character/1"}'])
def test_corrupt_record_never_reset(setup, bad):
    ctx, _, state = setup
    register(setup)
    path = state / "registry/toy/hero/character.json"
    path.write_text(bad)
    for action in (lambda: register(setup), lambda: records.get_character("toy", "hero", ctx=ctx)):
        with pytest.raises(ToolError) as err:
            action()
        assert err.value.error["code"] == "internal" and str(path) in str(err.value)
        assert path.read_text() == bad


def test_unknown_ids_and_cross_character_approval(setup):
    ctx, refs, _ = setup
    assert call("list_characters", {"project": "missing"}, ctx=ctx)["error"]["code"] == "not_found"
    error = call("register_character", {"project": "toy", "character": "new", "reference_dir": str(refs)}, ctx=ctx)[
        "error"
    ]
    assert error["code"] == "invalid_params" and "reviewed PR" in error["hint"]
    assert call("get_character", {"project": "toy", "character": "missing"}, ctx=ctx)["error"]["code"] == "not_found"
    assert call("get_artifact", {"ref": "import-missing"}, ctx=ctx)["error"]["code"] == "not_found"
    register(setup)
    with pytest.raises(ToolError, match="Unknown reference set"):
        records.approve_references("toy", "hero", "refs-" + "0" * 12, "Owner", "viewed", ctx=ctx)


@pytest.fixture
def imported(setup, job, monkeypatch):
    ctx, _, _ = setup
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", os.pathsep.join([str(job.parent), str(job.parent.parent)]))
    before = snapshot(job)
    imported = records.import_job("toy", "hero", str(job), ctx=ctx)
    assert snapshot(job) == before
    return ctx, job, imported


@pytest.mark.parametrize("state", ["pass", "fail", "none", "tamper", "report", "corrupt_report", "missing_report"])
def test_current_review_states(imported, state):
    ctx, job, imported = imported
    if state in {"pass", "tamper", "report", "corrupt_report", "missing_report"}:
        record_review(job, **PASS)
    elif state == "fail":
        record_review(job, **{**PASS, "verdict": "fail"})
    if state == "tamper":
        exported(job)[0].write_bytes(b"tampered")
    if state == "report":
        report = json.loads((job / "report.json").read_text())
        (job / "report.json").write_text(json.dumps({**report, "note": "changed"}))
    if state == "corrupt_report":
        (job / "report.json").write_text("{")
    if state == "missing_report":
        (job / "report.json").unlink()
    review = records.check_review(imported["job_id"], ctx=ctx)
    assert review["production_usable"] == (state == "pass")
    assert review["review_state"] == (
        "invalid" if state in {"tamper", "report", "corrupt_report", "missing_report"} else state
    )
    full = records.get_character("toy", "hero", ctx=ctx)
    assert full["jobs"][0]["human"]["production_usable"] == (state == "pass")


def test_import_artifact_metadata_and_escape_refusal(imported):
    ctx, job, entry = imported
    duplicate = records.import_job("toy", "hero", str(job), ctx=ctx)
    assert not duplicate["created"] and duplicate["artifact_id"] == entry["artifact_id"]
    target = exported(job)[0]
    relative = target.relative_to(job).as_posix()
    meta = records.get_artifact(entry["job_id"] + ":" + relative, ctx=ctx)
    assert meta["size"] == target.stat().st_size and meta["sha256"] == sha256_file(target)
    if target.suffix == ".png":
        assert meta["media_type"] == "image/png" and meta["dimensions"]
    assert records.get_artifact(entry["artifact_id"], ctx=ctx)["file_count"] > 0
    for relative in ["../toy.yaml", str(target), "raw/../../toy.yaml"]:
        with pytest.raises(ToolError) as err:
            records.get_artifact(entry["job_id"] + ":" + relative, ctx=ctx)
        assert err.value.error["code"] == "path_refused"
    (job / "escape.txt").symlink_to(job.parent / "palette.png")
    with pytest.raises(ToolError):
        records.get_artifact(entry["job_id"] + ":escape.txt", ctx=ctx)


def test_import_refuses_cross_project_and_corrupt_metadata(setup, job, monkeypatch):
    ctx, _, state = setup
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", str(job.parent.parent))
    other = state / "artifacts/other/hero/idle/job"
    shutil.copytree(job, other)
    with pytest.raises(ToolError, match="another project"):
        records.import_job("toy", "hero", str(other), ctx=ctx)
    (job / "manifest.json").write_text("{")
    with pytest.raises(ToolError, match=r"manifest\.json"):
        records.import_job("toy", "hero", str(job), ctx=ctx)


def test_owner_cli_only(setup, monkeypatch, capsys):
    ctx, _, _ = setup
    ref = register(setup)["ref_set_id"]
    monkeypatch.setattr(importlib.import_module("sonoranlab.api.registry"), "Context", lambda: ctx)
    assert (
        main(
            [
                "registry",
                "approve-references",
                "--project",
                "toy",
                "--character",
                "hero",
                "--ref-set",
                ref,
                "--reviewer",
                "Owner",
                "--evidence",
                "all eight stills inspected",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["schema"] == "sonoranlab.ref-approval/1"


def test_lineage_and_child_requires_own_review(imported):
    ctx, parent, entry = imported
    record_review(parent, **PASS)
    child = shutil.copytree(parent, parent.parent / "child")
    (child / "review.json").unlink()
    manifest = json.loads((child / "manifest.json").read_text())
    manifest["upstream"] = {"parent_job": str(parent)}
    (child / "manifest.json").write_text(json.dumps(manifest))
    child_entry = records.import_job("toy", "hero", str(child), ctx=ctx)
    history = records.get_character("toy", "hero", ctx=ctx)["jobs"]
    parent_record = next(j for j in history if j["job_id"] == entry["job_id"])
    child_record = next(j for j in history if j["job_id"] == child_entry["job_id"])
    assert parent_record["lineage"]["repair_children"] == [child_entry["job_id"]]
    assert child_record["lineage"]["parent_job"] == str(parent)
    assert parent_record["human"]["production_usable"]
    assert not child_record["human"]["production_usable"]


def test_existing_unindexed_set_directory_not_overwritten(setup):
    ctx, refs, state = setup
    files = {d: {"sha256": sha256_file(refs / f"still_{d}.png")} for d in ORDER}
    directory = state / "registry/toy/hero/refs" / records._ref_id(files)
    directory.mkdir(parents=True)
    (directory / "keep.txt").write_text("keep")
    result = call("register_character", {"project": "toy", "character": "hero", "reference_dir": str(refs)}, ctx=ctx)
    assert result["error"]["code"] == "not_found" and "character.json" in result["error"]["message"]
    assert (directory / "keep.txt").read_text() == "keep"


def test_retired_profile_row_stays_in_union(setup):
    ctx, _, _ = setup
    register(setup)
    path = ctx.repo / "profiles/toy.yaml"
    profile = yaml.safe_load(path.read_text())
    profile["characters"] = {}
    path.write_text(yaml.safe_dump(profile))
    character = records.list_characters("toy", ctx=ctx)["characters"][0]
    assert character["id"] == "hero" and character["registered"] and character["cell"] == [CELL, CELL]


def test_animate_wrapper_review_and_cross_project_provenance(setup, job, monkeypatch):
    ctx, _, _ = setup
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", str(job.parent.parent))
    outer = job.parent / "animate"
    current = shutil.copytree(job, outer / "sweep/final/job")
    manifest = json.loads((current / "manifest.json").read_text())
    animate = {
        "schema": "sonoranlab.animate/1",
        "outcome": "pass",
        "exit": 0,
        "inputs": {"profile": manifest["profile"], "character": "hero", "anim": "idle"},
        "verdict": {"pass": True, "job": str(current)},
    }
    (outer / "animate.json").write_text(json.dumps(animate))
    record_review(current, **PASS)
    entry = records.import_job("toy", "hero", str(outer), ctx=ctx)
    assert records.check_review(entry["job_id"], ctx=ctx)["production_usable"]
    other_profile = job.parent / "other.yaml"
    data = yaml.safe_load(Path(manifest["profile"]["path"]).read_text())
    data["id"] = "other"
    other_profile.write_text(yaml.safe_dump(data))
    manifest["profile"] = {"path": str(other_profile), "sha256": sha256_file(other_profile)}
    (current / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ToolError, match="requested project"):
        records.import_job("toy", "hero", str(outer), ctx=ctx)


def test_import_refuses_job_containing_game_checkout(setup, job, monkeypatch):
    ctx, _, _ = setup
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", str(job.parent.parent))
    path = ctx.repo / "profiles/toy.yaml"
    profile = yaml.safe_load(path.read_text())
    profile["export"]["godot_project"] = str(job / "private-game")
    path.write_text(yaml.safe_dump(profile))
    with pytest.raises(ToolError, match="contains a protected game"):
        records.import_job("toy", "hero", str(job), ctx=ctx)
