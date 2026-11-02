"""Saved-document final gate, using actual validation/export and a deterministic Godot stand-in."""

import importlib
import json
import shutil
import socket
import tempfile
from pathlib import Path

import numpy as np
import pytest
import yaml
from PIL import Image, PngImagePlugin
from test_broker import authorization, launcher
from test_interchange import GENERATE, MIRROR, ORDER, PALETTE, fixture, hashes  # noqa: F401

from sonoranlab import artifacts
from sonoranlab.artifacts import sha256_file
from sonoranlab.cli import main
from sonoranlab.export.godot import export_godot
from sonoranlab.profile import load_palette
from sonoranlab.repair import workspace as ws
from sonoranlab.repair.authorization import Denied, authorize, canonical, lease, persist
from sonoranlab.repair.broker import Broker, Journal, Snapshot
from sonoranlab.repair.compare import changed_coordinates, load_rgba, read_json
from sonoranlab.review import production_usable, record_review, review_errors
from sonoranlab.slice import slice_sheet
from sonoranlab.validate import read_frames, read_stills, validate

mod = importlib.import_module("sonoranlab.repair.finalize")


def godot_pass(sheet, **kwargs):
    with tempfile.TemporaryDirectory() as tmp:
        paths = export_godot(sheet, out_dir=Path(tmp), provenance={}, **kwargs)
        return {
            "pass": True,
            "godot": "fake",
            "godot_attempts": [{"step": "check", "returncode": 0}],
            "checked_sha256": {Path(paths[k]).name: sha256_file(Path(paths[k])) for k in ("png", "tres")},
            "result": {"pass": True},
        }


@pytest.fixture
def saved(fixture, monkeypatch):  # noqa: F811
    parent, profile, repair = fixture
    spec = profile.character("hero")
    # A real validator PASS: centred feet, stable silhouettes and unique true west stills.
    for path in [*(parent / "raw").glob("*.png"), *(parent / "processed/05_registered").glob("*.png")]:
        a = np.zeros((16, 16, 4), np.uint8)
        a[4:13, 6:10] = (*PALETTE[0], 255)
        a[5, 6] = (
            *PALETTE[ORDER.index(path.stem.removeprefix("still_")) if path.name.startswith("still_") else 1],
            255,
        )
        Image.fromarray(a).save(path)
    frames, _ = read_frames(parent / "processed/05_registered", ORDER)
    stills, _ = read_stills(parent / "raw", ORDER)
    report = validate(
        frames,
        stills=stills,
        cell=spec.cell,
        frames=2,
        directions=GENERATE,
        palette=load_palette(profile),
        foot_y=spec.foot_y,
        anchor_x=spec.anchor_x,
        padding=spec.padding,
        limits=spec.limits,
        max_colours=8,
        loop=True,
        mirror=MIRROR,
        registration={d: {"clipped_px": [0, 0], "shift_px": [0, 0]} for d in GENERATE},
    )
    assert report["pass"], report["failures"]
    (parent / "report.json").write_text(json.dumps(report))
    manifest = read_json(parent / "manifest.json")
    manifest["inputs"] = {p.name: sha256_file(p) for p in (parent / "raw").iterdir()}
    manifest["profile"] = {
        "path": str(profile.path),
        "sha256": sha256_file(profile.path),
    }  # as build_manifest writes it
    manifest["outputs"] = {
        k: v for k, v in hashes(parent).items() if k.startswith(("processed/", "export/")) or k == "report.json"
    }
    (parent / "manifest.json").write_text(json.dumps(manifest))
    (parent / "review.json").unlink()
    record_review(
        parent,
        verdict="pass",
        reviewer="parent human",
        cleanup_minutes=0,
        started_at="2026-10-20T12:00:00+00:00",
        ended_at="2026-10-20T12:00:00+00:00",
    )
    assert production_usable(parent)
    ws.prepare(parent, profile, "hero", "idle", repair, converter=ws.FakeConverter())
    auth = authorization(repair)
    auth["targets"] = [
        {"kind": "anim", "direction": "east", "index": 0, "mask": {"pixels": [[7, 6]]}, "derive_mirror": True}
    ]
    authorize(repair, auth)
    checkpoint_saved(repair)
    monkeypatch.setattr(mod, "check_in_godot", godot_pass)
    monkeypatch.setattr(mod, "AsepriteConverter", ws.FakeConverter)
    return repair, parent, profile, repair.parent / "child"


def frame_path(repair, kind="anim", direction="east", index=0):
    mapping = read_json(repair / "mapping.json")
    n = next(n for n, e in mapping.items() if (e["kind"], e["direction"], e["index"]) == (kind, direction, index))
    return repair / "workspace/fake-frames" / f"{n}.png"


def edit(repair, xy=(7, 6), colour=(*PALETTE[2], 255), **target):
    path = frame_path(repair, **target)
    a = load_rgba(path)
    x, y = xy
    a[y, x] = colour
    Image.fromarray(a).save(path)


def checkpoint_saved(repair):
    """Record the same document/pixel bindings as the broker's checkpoint."""
    frames = {}
    for path in (repair / "workspace/fake-frames").glob("*.png"):
        a = load_rgba(path)
        Broker.pixel_hash(a)  # Broker snapshots canonicalize hidden RGB.
        frames[path.stem] = a
    accepted = repair / "session/accepted.json"
    persist(accepted, Snapshot({"width": 16, "height": 16}, frames).packed())
    state = read_json(repair / "state.json")
    persist(
        repair / "state.json",
        {
            **state,
            "state": "checkpointed",
            "session_state": "STOPPED",
            "iterations": 1,
            "accepted_sha256": sha256_file(accepted),
            "document_sha256": sha256_file(repair / "workspace/doc.aseprite"),
        },
    )


def test_noop_is_exact_and_new_review_is_required(saved):
    repair, parent, profile, out = saved
    immutable = hashes(parent), hashes(repair / "source")
    assert main(["repair", "finalize", str(repair), "--out", str(out)]) == 0
    report = read_json(out / "report.json")
    assert report["pass"] and report["registration"] == read_json(parent / "report.json")["registration"]
    assert report["provenance"]["profile"]["sha256"] == sha256_file(profile.path)
    assert len(report["digests"]) == 18
    for path in (out / "raw").glob("*.png"):
        assert np.array_equal(load_rgba(path), load_rgba(repair / "source" / path.name))
    assert (hashes(parent), hashes(repair / "source")) == immutable
    rm = read_json(out / "repair-manifest.json")
    assert rm["schema"] == "sonoranlab.repair-manifest/1" and rm["human_review"] == "pending"
    assert not rm["changed"] and len(rm["unchanged"]) >= 18
    assert rm["authorization_sha256"] == sha256_file(repair / "authorization.json")
    assert rm["godot_check"]["godot_attempts"]
    assert (out / "repair-before.png").is_file() and (out / "repair-after.png").is_file()
    assert not production_usable(out) and not (out / "review.json").exists()
    assert main(["review", "check", str(out)]) == 1
    shutil.copyfile(parent / "review.json", out / "review.json")
    assert not production_usable(out)
    (out / "review.json").unlink()
    assert (
        main(
            [
                "review",
                "record",
                str(out),
                "--verdict",
                "pass",
                "--reviewer",
                "new human",
                "--cleanup-minutes",
                "0",
                "--started",
                "2026-10-20T12:00:00+00:00",
                "--ended",
                "2026-10-20T12:00:00+00:00",
            ]
        )
        == 0
    )
    assert production_usable(out)
    assert main(["review", "check", str(out)]) == 0
    (out / "export/hero/hero.png").write_bytes(b"changed")
    assert not production_usable(out)
    assert main(["review", "check", str(out)]) == 1


def test_permitted_edit_and_exact_mirror_true_west_still(saved):
    repair, _, _, out = saved
    edit(repair)
    checkpoint_saved(repair)
    result = mod.finalize(repair, out)
    assert result["ok"], result
    assert changed_coordinates(load_rgba(repair / "source/east_0.png"), load_rgba(out / "raw/east_0.png")) == [[7, 6]]
    sheet = load_rgba(Path(result["export"]["png"]))
    cells = slice_sheet(sheet, (16, 16), ORDER, 2, start_col=1)
    assert np.array_equal(cells["west"][0], cells["east"][0][:, ::-1])
    assert np.array_equal(sheet[32:48, :16], load_rgba(repair / "source/still_west.png"))
    rm = read_json(out / "repair-manifest.json")
    assert "east_0.png" in rm["changed"]
    assert next(v["changed"] for v in rm["target_diffs"].values()) == [[7, 6]]
    sidecar = read_json(Path(result["export"]["sidecar"]))
    assert sidecar["provenance"]["report_sha256"] == sha256_file(out / "report.json")


@pytest.mark.parametrize("damage", ["modify", "delete", "replace", "copy_job"])
def test_child_review_binds_lineage_and_location(saved, damage):
    repair, parent, _, out = saved
    assert mod.finalize(repair, out)["ok"]
    manifest = read_json(out / "manifest.json")
    assert manifest["repair"] == {
        "manifest": "repair-manifest.json",
        "repair_id": read_json(repair / "repair.json")["repair_id"],
        "parent_job": str(parent),
    }
    assert "repair-manifest.json" not in manifest["outputs"]
    sidecar = out / "repair-manifest.json"
    assert read_json(sidecar)["child_manifest_sha256"] == sha256_file(out / "manifest.json")
    record_review(
        out,
        verdict="pass",
        reviewer="child human",
        cleanup_minutes=0,
        started_at="2026-10-20T12:00:00+00:00",
        ended_at="2026-10-20T12:00:00+00:00",
    )
    assert production_usable(out)
    assert read_json(out / "review.json")["binds"]["repair_manifest_sha256"] == sha256_file(sidecar)
    if damage == "modify":
        doc = read_json(sidecar)
        doc["parent_job"] = "different parent"
        sidecar.write_text(json.dumps(doc))
    elif damage == "delete":
        sidecar.unlink()
    elif damage == "replace":
        sidecar.unlink()
        sidecar.write_text("{}\n")
    else:
        out = shutil.copytree(out, out.with_name("copied-child"))
    assert not production_usable(out)
    field = "binds.job" if damage == "copy_job" else "binds.repair_manifest_sha256"
    assert any(e["code"] == "REVIEW_BINDING_INVALID" and e["field"] == field for e in review_errors(out))


@pytest.mark.parametrize(
    "damage,code",
    [
        ("outside", "unauthorized_pixels"),
        ("other_frame", "unauthorized_pixels"),
        ("still", "unauthorized_pixels"),
        ("duration", "metadata_mismatch"),
        ("tag", "metadata_mismatch"),
        ("palette", "invalid_pixels"),
        ("alpha", "invalid_pixels"),
        ("missing_still", "frame_set_mismatch"),
    ],
)
def test_export_gate_retains_failed_evidence(saved, damage, code):
    repair, _, _, out = saved
    if damage in ("duration", "tag"):
        path = repair / "workspace/doc.aseprite"
        doc = read_json(path)
        if damage == "duration":
            doc["meta"]["durations_ms"][0] += 1
        else:
            doc["meta"]["tags"]["still"] = [1, 7]
        path.write_text(json.dumps(doc))
        checkpoint_saved(repair)
    elif damage == "missing_still":
        frame_path(repair, kind="still", direction="west").unlink()
    elif damage == "outside":
        edit(repair, (8, 6))
    elif damage == "other_frame":
        edit(repair, index=1)
    elif damage == "still":
        edit(repair, kind="still", direction="west")
    else:
        edit(repair, colour=(1, 2, 3, 255) if damage == "palette" else (*PALETTE[2], 128))
    result = mod.finalize(repair, out)
    assert not result["ok"] and result["error"] == code
    assert not read_json(out / "report.json")["pass"]
    assert read_json(out / "manifest.json")["result"] == {"pass": False, "exported": False, "exit": 1}
    assert not (out / "export").exists() and (out / "repair-manifest.json").is_file()


@pytest.mark.parametrize(
    "damage,code",
    [
        ("source", "source_hash_mismatch"),
        ("report", "parent_hash_mismatch"),
        ("export", "parent_hash_mismatch"),
        ("manifest", "parent_hash_mismatch"),
        ("review", "parent_hash_mismatch"),
        ("profile", "profile_hash_mismatch"),
        ("authorization", "authorization_hash_mismatch"),
        ("revision", "stale_source_revision"),
        ("tainted", "tainted"),
        ("iterations", "iteration_limit"),
        ("unknown", "outcome_unknown"),
        ("active", "session_not_stopped"),
        ("mirror", "mirror_derivation_required"),
    ],
)
def test_preflight_refuses_before_export_or_child(saved, monkeypatch, damage, code):
    repair, parent, profile, out = saved
    state = read_json(repair / "state.json")
    paths = {
        "source": repair / "source/east_0.png",
        "report": parent / "report.json",
        "manifest": parent / "manifest.json",
        "review": parent / "review.json",
        "export": parent / "export/hero/hero.tres",
        "profile": profile.path,
        "authorization": repair / "authorization.json",
    }
    if damage in paths:
        path = paths[damage]
        path.chmod(0o644)
        path.write_bytes(path.read_bytes() + b" ")
    elif damage in ("revision", "mirror"):
        auth = read_json(repair / "authorization.json")
        if damage == "revision":
            auth["source_revision"] = "0" * 64
        else:
            auth["targets"][0]["derive_mirror"] = False
        (repair / "authorization.json").chmod(0o644)
        (repair / "authorization.json").write_bytes(canonical(auth))
        state["authorization_sha256"] = sha256_file(repair / "authorization.json")
    elif damage == "tainted":
        state["session_state"] = "TAINTED"
    elif damage == "iterations":
        state["iterations"] = 3
    elif damage == "active":
        state.update(state="prepared", session_state="ACTIVE")
    else:
        journal = Journal(repair / "session/ops.jsonl", state, repair / "state.json")
        journal.append(event="outcome_unknown", op_id="lost")
        journal.close()
        state.update(state="DISCONNECTED", session_state="DISCONNECTED")
    persist(repair / "state.json", state)
    monkeypatch.setattr(ws.FakeConverter, "export", lambda *a: pytest.fail("preflight must run first"))
    with pytest.raises(Denied, match=code):
        mod.finalize(repair, out)
    assert not out.exists()


@pytest.mark.parametrize(
    "destination,code",
    [
        ("repo", "repository_path"),
        ("checkout", "git_worktree"),
        ("parent", "forbidden_path"),
        ("repair", "forbidden_path"),
        ("game", "forbidden_path"),
        ("other_job", "nested_workspace"),
        ("other_repair", "nested_workspace"),
        ("outside", "outside_root"),
        ("symlink", "symlink_path"),
        ("existing", "destination_exists"),
        ("existing_file", "destination_exists"),
    ],
)
def test_unsafe_or_existing_output(saved, tmp_path, monkeypatch, capsys, destination, code):
    repair, parent, profile, out = saved
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(artifacts, "_REPO", repo)
    checkout = tmp_path / "game-checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git/HEAD").write_text("ref: refs/heads/main\n")
    for name, marker in (("other_job", "manifest.json"), ("other_repair", "repair.json")):
        area = tmp_path / name
        area.mkdir()
        (area / marker).write_text("{}")
    (tmp_path / "link").symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setenv("SONORANLAB_REPAIR_ROOT", str(tmp_path))
    out = {
        "repo": repo / "child",
        "checkout": checkout / "nested/child",
        "parent": parent / "child",
        "repair": repair / "child",
        "game": profile.export.godot_project / "child",
        "other_job": tmp_path / "other_job/nested/child",
        "other_repair": tmp_path / "other_repair/nested/child",
        "outside": tmp_path.parent / "child",
        "symlink": tmp_path / "link/child",
        "existing": out,
        "existing_file": out,
    }[destination]
    if destination == "existing":
        out.mkdir()
    elif destination == "existing_file":
        out.write_text("keep")
    before = set(tmp_path.rglob("*")), hashes(tmp_path)
    monkeypatch.setattr(mod, "lease", lambda *a: pytest.fail("destination check must precede lease creation"))
    assert main(["repair", "finalize", str(repair), "--out", str(out)]) == 1
    assert json.loads(capsys.readouterr().out)["message"].startswith(f"{code}:")
    assert (set(tmp_path.rglob("*")), hashes(tmp_path)) == before


def test_lease_is_held_until_finalize_returns(saved):
    repair, _, _, out = saved
    with lease(repair), pytest.raises(Denied, match="lease_held"):
        mod.finalize(repair, out)
    assert not out.exists()


def test_full_animation_revalidated_and_limits_unchanged(saved, monkeypatch):
    repair, _, profile, out = saved
    original = mod.validate

    def unrelated_failure(clips, **kwargs):
        assert set(clips) == set(GENERATE) and all(len(fs) == 2 for fs in clips.values())
        assert len(kwargs["stills"]) == 8
        assert kwargs["limits"] == profile.character("hero").limits
        assert kwargs["registration"]["north"]["clipped_px"] == [0, 0]
        rep = original(clips, **kwargs)
        rep["pass"] = False
        rep["failures"].append("unrelated north frame failed")
        return rep

    monkeypatch.setattr(mod, "validate", unrelated_failure)
    result = mod.finalize(repair, out)
    assert not result["ok"] and result["error"] == "validation_failed"
    assert not (out / "export").exists()


def test_cli_refusal_is_json_exit_one(saved, capsys):
    repair, _, _, out = saved
    out.mkdir()
    assert main(["repair", "finalize", str(repair), "--out", str(out)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "refused" and result["message"].startswith("destination_exists:")
    with pytest.raises(SystemExit) as exc:
        main(["repair", "finalize", str(repair)])
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "usage_error"


@pytest.mark.parametrize("failure", ["checker", "hash"])
def test_godot_failure_never_publishes_export(saved, monkeypatch, failure):
    repair, _, _, out = saved

    def failed(*args, **kwargs):
        result = godot_pass(*args, **kwargs)
        if failure == "checker":
            result["pass"] = False
        else:
            result["checked_sha256"] = {}
        return result

    monkeypatch.setattr(mod, "check_in_godot", failed)
    result = mod.finalize(repair, out)
    assert not result["ok"] and not (out / "export").exists()
    assert not read_json(out / "report.json")["pass"]


@pytest.mark.skipif(shutil.which("godot") is None, reason="godot is not on PATH")
def test_real_godot_roundtrip(saved, monkeypatch):
    from sonoranlab.pipeline import check_in_godot

    repair, _, _, out = saved
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
    except OSError as e:
        pytest.skip(f"Godot editor import needs local sockets: {e}")
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        path = out.parent / var.lower()
        path.mkdir()
        monkeypatch.setenv(var, str(path))
    monkeypatch.setattr(mod, "check_in_godot", check_in_godot)
    result = mod.finalize(repair, out)
    assert result["ok"], result
    gc = read_json(out / "repair-manifest.json")["godot_check"]
    assert gc["pass"] and gc["godot_attempts"]


@pytest.mark.parametrize("damage", ["edit", "truncate"])
def test_journal_corruption_refused(saved, damage):
    repair, _, _, out = saved
    state = read_json(repair / "state.json")
    path = repair / "session/ops.jsonl"
    journal = Journal(path, state, repair / "state.json")
    journal.append(event="session_start")
    journal.close()
    row = json.loads(path.read_text())
    row["event"] = "tampered"
    path.write_text(json.dumps(row) + "\n" if damage == "edit" else "")
    with pytest.raises(Denied, match="log_corrupt"):
        mod.finalize(repair, out)
    assert not out.exists()


def test_resolved_unknown_operation_summary_and_new_snapshot(saved):
    repair, _, _, out = saved
    # Earlier snapshots may be failed attempts; finalize always exports again.
    (repair / "workspace/export").mkdir()
    state = read_json(repair / "state.json")
    journal = Journal(repair / "session/ops.jsonl", state, repair / "state.json")
    target = {"kind": "anim", "direction": "east", "index": 0}
    journal.append(event="dispatch", op_id="one", request={"action": "op", "op": "draw.pixels", "target": target})
    journal.append(event="outcome_unknown", op_id="one")
    journal.append(
        event="verification",
        op_id="one",
        response={"ok": True, "outcome": "applied_within_mask", "changed": {"13": [[7, 6]]}},
    )
    journal.close()
    edit(repair)
    checkpoint_saved(repair)
    result = mod.finalize(repair, out)
    assert result["ok"], result
    assert (repair / "workspace/export-0002/export.json").is_file()
    rm = read_json(out / "repair-manifest.json")
    assert rm["operations"]["sha256"] == sha256_file(repair / "session/ops.jsonl")
    assert rm["operations"]["operations"] == [
        {"op_id": "one", "op": "draw.pixels", "target": target, "verdict": "applied_within_mask", "changed_count": 1}
    ]
    assert rm["frames"]["west_0.png"]["before"] != rm["frames"]["west_0.png"]["after"]


def test_failed_batch_export_still_writes_failed_child(saved, monkeypatch):
    from sonoranlab.repair import EditorUnavailable

    repair, _, _, out = saved

    def unavailable():
        raise EditorUnavailable("test runtime absent")

    monkeypatch.setattr(mod, "AsepriteConverter", unavailable)
    result = mod.finalize(repair, out)
    assert not result["ok"] and result["error"] == "editor_unavailable"
    assert not read_json(out / "report.json")["pass"]
    assert (out / "repair-manifest.json").exists() and not (out / "export").exists()


def test_hidden_rgb_is_equivalent_and_iteration_cap_is_inclusive(saved):
    repair, _, _, out = saved
    edit(repair, xy=(0, 0), colour=(12, 34, 56, 0))
    state = read_json(repair / "state.json")
    state["iterations"] = 2
    persist(repair / "state.json", state)
    journal = Journal(repair / "session/ops.jsonl", state, repair / "state.json")
    journal.append(event="request", request=["rejected malformed client input"])
    journal.append(event="decision", response={"ok": False, "error": "invalid_request"})
    journal.close()
    result = mod.finalize(repair, out)
    assert result["ok"], result
    assert not read_json(out / "repair-manifest.json")["changed"]


def test_profile_swap_bound_to_parent_manifest(saved):
    repair, _, profile, out = saved
    data = yaml.safe_load(profile.path.read_text())
    data["characters"]["hero"]["max_foot_slide_px"] = 99
    replacement = profile.path.with_name("loose.yaml")
    replacement.write_text(yaml.safe_dump(data))
    doc = read_json(repair / "repair.json")
    doc["profile"] = {"path": str(replacement), "sha256": sha256_file(replacement)}
    persist(repair / "repair.json", doc)
    with pytest.raises(Denied, match="profile_hash_mismatch"):
        mod.finalize(repair, out)
    assert not out.exists()


def test_only_compared_files_are_ingested(saved, monkeypatch):
    repair, _, _, out = saved
    original = mod.compare

    def race(repair, exported):
        result = original(repair, exported)
        path = exported / "mapped/north_0.png"
        a = load_rgba(path)
        a[6, 7] = (*PALETTE[2], 255)
        Image.fromarray(a).save(path)
        return result

    monkeypatch.setattr(mod, "compare", race)
    assert mod.finalize(repair, out)["ok"]
    assert np.array_equal(load_rgba(out / "raw/north_0.png"), load_rgba(repair / "source/north_0.png"))


@pytest.mark.parametrize("stage", ["compare", "validation", "godot"])
@pytest.mark.parametrize(
    "name,xy,code",
    [
        ("north_0.png", (7, 6), "unauthorized_pixels"),
        ("still_west.png", (7, 6), "unauthorized_pixels"),
        ("east_0.png", (8, 6), "unauthorized_pixels"),
        ("east_0.png", (7, 6), "checkpoint_mismatch"),
    ],
)
def test_changed_files_cannot_pass_final_gate(saved, monkeypatch, stage, name, xy, code):
    repair, _, _, out = saved
    attr = {"compare": "compare", "validation": "validate", "godot": "check_in_godot"}[stage]
    original = getattr(mod, attr)

    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        if stage == "compare":
            mapping = read_json(repair / "mapping.json")
            n = next(n for n, e in mapping.items() if mod.source_name(e) == name)
            path = args[1] / f"{n}.png"
        else:
            path = out / "raw" / name
        path.chmod(0o644)
        a = load_rgba(path)
        x, y = xy
        a[y, x] = (*PALETTE[2], 255)
        Image.fromarray(a).save(path)
        return result

    monkeypatch.setattr(mod, attr, race)
    result = mod.finalize(repair, out)
    assert not result["ok"] and code in {e["code"] for e in result["errors"]}
    assert not read_json(out / "report.json")["pass"]
    assert not (out / "export").exists()


@pytest.mark.parametrize("damage", ["document", "accepted", "missing_accepted", "pixels"])
def test_checkpoint_bindings_are_required(saved, damage):
    repair, _, _, out = saved
    if damage == "pixels":
        edit(repair)  # Even an authorized pixel must be accepted and checkpointed.
        result = mod.finalize(repair, out)
        assert not result["ok"] and result["error"] == "checkpoint_mismatch"
        assert not (out / "export").exists()
        return
    path = repair / ("workspace/doc.aseprite" if damage == "document" else "session/accepted.json")
    if damage == "missing_accepted":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(Denied, match="document_hash_mismatch" if damage == "document" else "accepted_hash_mismatch"):
        mod.finalize(repair, out)
    assert not out.exists()


@pytest.mark.parametrize("damage", ["none", "pixels", "document", "missing_prepare_hash"])
def test_prepare_without_session_requires_original_document_and_noop(saved, damage):
    repair, _, _, out = saved
    state = read_json(repair / "state.json")
    persist(
        repair / "state.json",
        {"state": "prepared", "iterations": 0, "authorization_sha256": state["authorization_sha256"]},
    )
    (repair / "session/accepted.json").unlink()
    assert read_json(repair / "repair.json")["document_sha256"] == sha256_file(repair / "workspace/doc.aseprite")
    if damage == "pixels":
        edit(repair)
    elif damage == "document":
        path = repair / "workspace/doc.aseprite"
        path.write_bytes(path.read_bytes() + b" ")
    elif damage == "missing_prepare_hash":
        doc = read_json(repair / "repair.json")
        del doc["document_sha256"]
        persist(repair / "repair.json", doc)
    if damage in ("document", "missing_prepare_hash"):
        with pytest.raises(Denied, match="document_hash_mismatch"):
            mod.finalize(repair, out)
        assert not out.exists()
    else:
        result = mod.finalize(repair, out)
        assert result["ok"] == (damage == "none")
        assert result["error"] == ("session_required" if damage == "pixels" else None)


def test_checkpointed_active_session_with_free_lease_is_refused(saved):
    repair, _, _, out = saved
    state = read_json(repair / "state.json")
    persist(repair / "state.json", {**state, "session_state": "ACTIVE"})
    with pytest.raises(Denied, match="session_not_stopped"):
        mod.finalize(repair, out)
    assert not out.exists()


def test_parent_exports_must_match_manifest_even_if_repair_hash_updated(saved):
    repair, parent, _, out = saved
    path = parent / "export/hero/hero.tres"
    path.write_text("changed after prepare")
    doc = read_json(repair / "repair.json")
    doc["parent_hashes"]["export/hero/hero.tres"] = sha256_file(path)
    persist(repair / "repair.json", doc)
    with pytest.raises(Denied, match="parent_hash_mismatch"):
        mod.finalize(repair, out)
    assert not out.exists()


def test_source_reverified_before_evidence(saved, monkeypatch):
    repair, _, _, out = saved
    original = mod.check_in_godot
    before = load_rgba(repair / "source/north_0.png")

    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        path = repair / "source/north_0.png"
        path.chmod(0o644)
        a = load_rgba(path)
        a[6, 7] = (*PALETTE[2], 255)
        Image.fromarray(a).save(path)
        return result

    monkeypatch.setattr(mod, "check_in_godot", race)
    result = mod.finalize(repair, out)
    assert not result["ok"] and result["error"] == "source_hash_mismatch"
    assert not (out / "export").exists()
    assert (
        read_json(out / "repair-manifest.json")["frames"]["north_0.png"]["before"]
        == mod.hashlib.sha256(before.tobytes()).hexdigest()
    )


@pytest.mark.parametrize("chunk", ["gAMA", "iCCP", "sRGB", "cHRM"])
def test_colour_chunks_refused_and_recorded_before_raw_ingest(saved, monkeypatch, chunk):
    repair, _, _, out = saved
    original = ws.FakeConverter.export

    def with_colour_chunks(self, workspace, output):
        original(self, workspace, output)
        path = output / "1.png"
        if chunk == "iCCP":
            Image.fromarray(load_rgba(path)).save(path, icc_profile=b"ICC profile")
            return
        info = PngImagePlugin.PngInfo()
        data = {
            "gAMA": bytes.fromhex("0000b18f"),
            "sRGB": b"\0",
            "cHRM": bytes(32),
        }[chunk]
        info.add(chunk.encode(), data)
        Image.fromarray(load_rgba(path)).save(path, pnginfo=info)

    monkeypatch.setattr(ws.FakeConverter, "export", with_colour_chunks)
    result = mod.finalize(repair, out)
    assert not result["ok"] and result["error"] == "colour_chunks"
    assert result["errors"][0]["files"]["1.png"][0]["type"] == chunk
    assert not list((out / "raw").iterdir())
    assert not (out / "export").exists()
    rm = read_json(out / "repair-manifest.json")
    assert rm["comparison"]["png_chunks"]["1.png"][0]["type"] == chunk


def test_malformed_export_writes_failed_child(saved):
    repair, _, _, out = saved
    frame_path(repair).write_bytes(b"not a PNG")
    result = mod.finalize(repair, out)
    assert not result["ok"] and result["error"] == "export_failed"
    assert not read_json(out / "report.json")["pass"]
    assert (out / "repair-manifest.json").is_file() and not (out / "export").exists()


def test_incorrect_mirror_cannot_pass_even_when_godot_checks_it(saved, monkeypatch):
    repair, _, _, out = saved

    def wrong_mirror(sheet, **kwargs):
        sheet[ORDER.index("west") * 16 + 6, 16 + 7] = (*PALETTE[2], 255)
        return godot_pass(sheet, **kwargs)

    monkeypatch.setattr(mod, "check_in_godot", wrong_mirror)
    result = mod.finalize(repair, out)
    assert not result["ok"] and result["error"] == "mirror_mismatch"
    assert not (out / "export").exists()


def test_broker_checkpoint_then_stop_then_finalize(saved):
    repair, _, _, out = saved
    state = read_json(repair / "state.json")
    persist(
        repair / "state.json",
        {"state": "prepared", "iterations": 0, "authorization_sha256": state["authorization_sha256"]},
    )
    broker = Broker(repair, launcher=launcher(), converter=ws.FakeConverter(), ready_timeout=0.5)
    try:
        assert broker.handle(
            {
                "action": "op",
                "token": broker.token,
                "op_id": "one",
                "source_revision": broker.auth["source_revision"],
                "target": {"kind": "anim", "direction": "east", "index": 0},
                "op": "draw.pixels",
                "args": {"color": broker.doc["palette"][2], "points": [{"x": 7, "y": 6}]},
            }
        )["ok"]
        assert broker.handle({"action": "stop", "token": broker.token, "checkpoint": True})["ok"]
    finally:
        broker.close()
    result = mod.finalize(repair, out)
    assert result["ok"], result
    assert read_json(out / "repair-manifest.json")["operations"]["had_session"]


@pytest.mark.parametrize("equivalent", [True, False])
def test_profile_changed_only_in_another_row_is_finalized_by_the_shared_rule(saved, monkeypatch, equivalent):
    """finalize uses import/prepare's equivalence when the repair's profile differs from the parent's recorded one."""
    repair, _, profile, out = saved
    seen = []

    def rule(current, recorded, rev, bound):
        seen.append(recorded.get("sha256"))
        return equivalent

    monkeypatch.setattr(mod, "same_character_profile", rule)
    with open(profile.path, "a") as f:
        f.write("# another character's row changed after the parent ran\n")
    doc = read_json(repair / "repair.json")
    recorded = doc["profile"]["sha256"]
    doc["profile"]["sha256"] = sha256_file(profile.path)
    persist(repair / "repair.json", doc)
    if equivalent:
        try:
            mod.finalize(repair, out)
        except Denied as exc:
            assert "profile_hash_mismatch" not in str(exc)
    else:
        with pytest.raises(Denied, match="profile_hash_mismatch"):
            mod.finalize(repair, out)
    assert seen == [recorded]
