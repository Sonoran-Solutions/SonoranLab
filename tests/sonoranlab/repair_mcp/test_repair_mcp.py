"""Optional handoff, real acceptance checks and transport parity, without editor binaries."""

import importlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from mcp import types
from test_broker import authorization, launcher
from test_finalize import edit, saved  # noqa: F401
from test_interchange import ORDER, hashes
from test_interchange import fixture as parent_fixture  # noqa: F401
from test_stdio import AUDIT, run_async

from sonoranlab import registry as records
from sonoranlab.api import call, paths, registry
from sonoranlab.api.registry import Context
from sonoranlab.api.tools import repair as ops
from sonoranlab.cli import main
from sonoranlab.jobs import manager, store, worker
from sonoranlab.jobs.kinds import repair as kinds
from sonoranlab.mcp import server
from sonoranlab.profile import load_profile
from sonoranlab.repair import workspace
from sonoranlab.repair.authorization import authorize, persist
from sonoranlab.repair.broker import Broker
from sonoranlab.repair.compare import changed_coordinates, load_rgba, read_json
from sonoranlab.review import production_usable, review_errors

BASE = {"project": "toy", "character": "hero", "anim": "idle"}
OPTIONAL = {"prepare_repair", "inspect_repair", "inspect_repair_diff", "finalize_repair", "abort_repair"}


@pytest.fixture
def fixture(parent_fixture, monkeypatch):  # noqa: F811
    parent, profile, repair = parent_fixture
    root = repair.parent / "repairs"
    root.mkdir()
    monkeypatch.setenv("SONORANLAB_REPAIR_ROOT", str(root))
    profiles = profile.path.parent / "repo/profiles"
    profiles.mkdir(parents=True)
    shutil.copyfile(profile.palette.source, profiles / "palette.png")
    shutil.copyfile(profile.path, profiles / "toy.yaml")
    manifest = read_json(parent / "manifest.json")
    manifest["schema"] = "sonoranlab.manifest/1"
    (parent / "manifest.json").write_text(json.dumps(manifest))
    return parent, load_profile(profiles / "toy.yaml"), root / repair.name


@pytest.fixture
def setup(saved, monkeypatch):  # noqa: F811
    repair, parent, profile, _ = saved
    ctx = Context(profile.path.parent.parent)
    monkeypatch.setenv("SONORANLAB_STATE", str(repair.parent.parent / "state"))
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", str(repair.parent.parent))
    runtime = repair.parent / "fake-runtime"
    runtime.mkdir()
    monkeypatch.setenv("SONORANLAB_EDITOR_RUNTIME", str(runtime))
    monkeypatch.setenv("SONORANLAB_JOB_LAUNCHER", "popen")
    monkeypatch.setattr(importlib.import_module("sonoranlab.api.registry"), "Context", lambda: ctx)
    monkeypatch.setattr(paths, "Context", lambda: ctx)
    monkeypatch.setattr(kinds, "Context", lambda: ctx)
    monkeypatch.setattr(workspace, "AsepriteConverter", workspace.FakeConverter)
    monkeypatch.setattr(manager, "launch", lambda job: None)
    imported = records.import_job("toy", "hero", str(parent), ctx=ctx)
    return SimpleNamespace(repair=repair, parent=parent, profile=profile, ctx=ctx, imported=imported)


def invoke(setup, name, **params):
    result = call(name, params, ctx=setup.ctx)
    assert result["ok"], result
    return result["result"]


def finalize(setup, repair=None):
    queued = invoke(setup, "finalize_repair", repair_id=(repair or setup.repair).name)
    assert queued["state"] == "queued"
    worker._work(store.job_dir(queued["job_id"]))
    job = manager.get(queued["job_id"])
    assert job["state"] == "succeeded", job
    return job, Path(job["result"]["child_path"])


def test_lifecycle_contained_fake_edits_and_new_review(setup):
    parent_before = hashes(setup.parent)
    prepared = invoke(setup, "prepare_repair", **BASE, parent_job=setup.imported["artifact_id"] + ":manifest.json")
    repair = ops.repair_path(prepared["repair_id"])
    assert repair.name != read_json(repair / "repair.json")["repair_id"]
    assert prepared["identity"] == ops.identity(repair, read_json(repair / "repair.json"))
    assert len(prepared["staged_source_paths"]) == 18
    assert "sonoranlab repair authorize" in prepared["next_steps"]["owner_authorization"]
    assert not (repair / "authorization.json").exists()
    assert not (repair / "session/lease.lock").exists()
    auth = authorization(repair)
    auth["operations"] = ["draw.pixels", "draw.grid", "draw.clear", "draw.blit"]
    auth["targets"] = [
        {"kind": "anim", "direction": "east", "index": 0, "mask": {"pixels": [[7, 6]]}, "derive_mirror": True}
    ]
    # Owner-side library/CLI authority is separate from the tool registry.
    authorize(repair, auth)
    broker = Broker(repair, launcher=launcher(), converter=workspace.FakeConverter(), ready_timeout=0.5)
    try:
        info = invoke(setup, "inspect_repair", repair_id=repair.name)
        assert info["session"]["lease_held"]
        assert info["authorization"]["operations"] == auth["operations"]
        assert info["authorization"]["targets"] == auth["targets"]
        assert not call("abort_repair", {"repair_id": repair.name}, ctx=setup.ctx)["ok"]
        assert broker.handle(
            {
                "action": "op",
                "token": broker.token,
                "op_id": "repair-one-pixel",
                "source_revision": broker.auth["source_revision"],
                "target": {"kind": "anim", "direction": "east", "index": 0},
                "op": "draw.pixels",
                "args": {"color": broker.doc["palette"][2], "points": [{"x": 7, "y": 6}]},
            }
        )["ok"]
        assert broker.handle({"action": "stop", "token": broker.token, "checkpoint": True})["ok"]
    finally:
        broker.close()
    info = invoke(setup, "inspect_repair", repair_id=repair.name)
    assert not info["session"]["lease_held"] and info["journal"]["events"] > 0
    before = hashes(repair)
    diff = invoke(setup, "inspect_repair_diff", repair_id=repair.name)
    assert hashes(repair) == before
    assert not diff["unauthorized_changes"]
    assert next(frame for frame in diff["frames"].values() if frame["changed_count"])["changed"] == [[7, 6]]
    job, child = finalize(setup, repair)
    result = job["result"]
    assert result["technical"] == {"validator": "pass", "export": "pass", "godot": "pass"}
    assert child == repair.parent / f"{repair.name}-child-{job['job_id']}"
    assert store.state_root().parent == child.parent.parent
    assert not store.state_root().is_relative_to(child.parent)
    assert result["artifact_id"].startswith("import-") and result["hashes"]
    assert result["artifact_id"] != setup.imported["artifact_id"]
    output = store.output(job)
    assert list(output.iterdir()) == [output / "finalize-result.json"]
    record = read_json(output / "finalize-result.json")
    assert record == {key: result[key] for key in ("child_path", "artifact_id", "hashes", "technical", "human")}
    review = records.check_review(result["artifact_id"], ctx=setup.ctx)
    assert result["human"]["review"] == review["review_state"]
    assert result["human"]["problems"] == review["problems"]
    for ref in [
        *(result[key] for key in ("report", "contact", "manifest", "repair_manifest")),
        *result["export"].values(),
    ]:
        assert ref.startswith(result["artifact_id"] + ":")
        artifact = invoke(setup, "get_artifact", ref=ref)
        assert artifact["sha256"] == hashes(child)[ref.split(":", 1)[1]]
    assert result["human"]["status"] == "awaiting_owner_review"
    assert result["human"]["review"] == "none" and not result["human"]["production_usable"]
    assert not production_usable(child) and review_errors(child)
    assert not (child / "review.json").exists()
    for direction in ORDER:
        assert np.array_equal(
            load_rgba(child / f"raw/still_{direction}.png"), load_rgba(repair / f"source/still_{direction}.png")
        )
    for original in (repair / "source").glob("*.png"):
        changed = changed_coordinates(load_rgba(original), load_rgba(child / "raw" / original.name))
        assert changed == ([[7, 6]] if original.name == "east_0.png" else [])
    assert hashes(setup.parent) == parent_before
    again = call("finalize_repair", {"repair_id": repair.name}, ctx=setup.ctx)
    assert not again["ok"] and again["error"]["code"] == "refused"
    status = invoke(setup, "job_status", job_id=job["job_id"])
    assert status["human"] == {"review": "none", "production_usable": False}


@pytest.mark.parametrize("damage", ["aborted", "stale_source", "stale_parent", "stale_auth", "tainted"])
def test_stale_or_aborted_repair_refused(setup, damage):
    repair = setup.repair
    if damage == "aborted":
        invoke(setup, "abort_repair", repair_id=repair.name)
    elif damage == "stale_source":
        (repair / "source/east_0.png").chmod(0o644)
        (repair / "source/east_0.png").write_bytes(b"changed")
    elif damage == "stale_parent":
        assert production_usable(setup.parent)
        (setup.parent / "export/hero/hero.tres").write_bytes(b"edited after review")
        assert not production_usable(setup.parent)
    elif damage == "stale_auth":
        (repair / "authorization.json").chmod(0o644)
        (repair / "authorization.json").write_text("{}")
    else:
        state = read_json(repair / "state.json")
        persist(repair / "state.json", {**state, "state": "TAINTED"})
    result = call("finalize_repair", {"repair_id": repair.name}, ctx=setup.ctx)
    assert not result["ok"] and result["error"]["code"] == "refused", result
    assert not list((paths.state_root() / "artifacts").glob("*/*/*/*"))


@pytest.mark.parametrize("identifier", ["../repair", "/tmp/repair", "a/b", "..", ".", "a\\b", "repair\n", "x" * 129])
def test_repair_id_is_never_a_path(setup, identifier):
    for name in OPTIONAL - {"prepare_repair"}:
        result = call(name, {"repair_id": identifier}, ctx=setup.ctx)
        assert not result["ok"] and result["error"]["code"] == "invalid_params", result


def test_symlink_id_cannot_escape_root(setup, tmp_path):
    (setup.repair.parent / "escape").symlink_to(setup.repair, target_is_directory=True)
    for name in OPTIONAL - {"prepare_repair"}:
        result = call(name, {"repair_id": "escape"}, ctx=setup.ctx)
        assert not result["ok"] and result["error"]["code"] == "refused", result


def test_frozen_inputs_rechecked_after_queue(setup):
    queued = invoke(setup, "finalize_repair", repair_id=setup.repair.name)
    edit(setup.repair)
    worker._work(store.job_dir(queued["job_id"]))
    job = manager.get(queued["job_id"])
    assert job["state"] == "failed"
    assert not (store.output(job) / "manifest.json").exists()


def test_child_import_outside_read_roots_fails_with_registry_error(setup, monkeypatch):
    queued = invoke(setup, "finalize_repair", repair_id=setup.repair.name)
    roots = os.pathsep.join((str(setup.parent), str(setup.ctx.repo)))
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", roots)
    worker._work(store.job_dir(queued["job_id"]))
    job = manager.get(queued["job_id"])
    assert job["state"] == "failed"
    assert job["error"]["code"] == "path_refused"
    assert job["error"]["message"] == "Path is outside the permitted roots"
    assert os.environ["SONORANLAB_READ_ROOTS"] == roots
    assert not list(store.output(job).iterdir())
    child = setup.repair.parent / f"{setup.repair.name}-child-{job['job_id']}"
    assert (child / "repair-manifest.json").is_file()
    assert read_json(setup.repair / "mcp-finalize.json") == {"job_id": job["job_id"]}
    assert call("finalize_repair", {"repair_id": setup.repair.name}, ctx=setup.ctx)["error"]["code"] == "refused"


def test_finalize_result_record_is_exclusive(setup):
    queued = invoke(setup, "finalize_repair", repair_id=setup.repair.name)
    path = store.output(manager.get(queued["job_id"])) / "finalize-result.json"
    store.atomic(path, {"existing": True}, exclusive=True)
    worker._work(store.job_dir(queued["job_id"]))
    assert manager.get(queued["job_id"])["state"] == "failed"
    assert read_json(path) == {"existing": True}
    child = setup.repair.parent / f"{setup.repair.name}-child-{queued['job_id']}"
    assert (child / "repair-manifest.json").is_file()


def test_duplicate_queued_finalize_refused(setup):
    first = invoke(setup, "finalize_repair", repair_id=setup.repair.name)
    second = call("finalize_repair", {"repair_id": setup.repair.name}, ctx=setup.ctx)
    assert second["error"]["code"] == "conflict"
    assert first["job_id"] in second["error"]["message"]
    assert kinds.EXECUTORS["finalize_repair"].heavy_cpu


def test_missing_godot_retains_failed_child_and_separate_verdicts(setup, monkeypatch):
    from sonoranlab.pipeline import PipelineError

    module = importlib.import_module("sonoranlab.repair.finalize")

    def unavailable(*args, **kwargs):
        raise PipelineError("--godot-check: godot not found on PATH")

    monkeypatch.setattr(module, "check_in_godot", unavailable)
    monkeypatch.setattr(kinds.shutil, "which", lambda name: None)
    job, child = finalize(setup)
    assert not job["result"]["ok"]
    assert job["result"]["technical"] == {"validator": "pass", "export": "fail", "godot": "unavailable"}
    assert not job["result"]["human"]["production_usable"]
    assert not (child / "export").exists()
    assert not read_json(child / "report.json")["pass"]


@pytest.mark.parametrize("name", ["lease.lock", "ops.jsonl"])
def test_inspect_special_files_refused_without_blocking(setup, name):
    path = setup.repair / "session" / name
    path.unlink(missing_ok=True)
    os.mkfifo(path)
    result = call("inspect_repair", {"repair_id": setup.repair.name}, ctx=setup.ctx)
    assert result["error"]["code"] == "refused"


def test_diff_caps_coordinates_and_reports_unauthorized(setup, monkeypatch):
    comparison = importlib.import_module("sonoranlab.repair.compare")
    coords = [[0, 0]] * 2501
    monkeypatch.setattr(
        comparison,
        "diff",
        lambda repair: {"frames": {"1": {"changed": coords, "invalid_alpha": coords, "off_palette": coords}}},
    )
    before = hashes(setup.repair)
    result = invoke(setup, "inspect_repair_diff", repair_id=setup.repair.name)
    assert result["frames"]["1"]["changed_count"] == 2501
    assert len(result["frames"]["1"]["changed"]) == 2000
    assert result["unauthorized_changes"][0]["count"] == 2501
    assert len(result["unauthorized_changes"][0]["coordinates"]) == 2000
    assert hashes(setup.repair) == before


def test_cli_mcp_and_repair_cli_parity(setup, capsys, monkeypatch):
    # The saved fixture is checkpointed; the finalizer's shared batch exporter admits saved documents.
    module = importlib.import_module("sonoranlab.repair.finalize")
    module._export(setup.repair, workspace.FakeConverter(), read_json(setup.repair / "mapping.json"))
    for name in ("inspect_repair", "inspect_repair_diff"):
        params = {"repair_id": setup.repair.name}
        expected = call(name, params, ctx=setup.ctx)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(params)))
        assert main(["api", name, "--params", "-"]) == 0
        assert json.loads(capsys.readouterr().out) == expected

        async def exercise(name=name, params=params, expected=expected):
            result = await server.call_tool(None, types.CallToolRequestParams(name=name, arguments=params))
            assert result.structured_content == expected
            assert json.loads(result.content[0].text) == expected

        run_async(exercise)
    assert main(["repair", "diff", str(setup.repair)]) == 0
    cli_diff = json.loads(capsys.readouterr().out)
    mcp_diff = invoke(setup, "inspect_repair_diff", repair_id=setup.repair.name)
    assert cli_diff["frames"]["1"]["changed"] == mcp_diff["frames"]["1"]["changed"]
    before = hashes(setup.repair)
    info = invoke(setup, "inspect_repair", repair_id=setup.repair.name)
    assert main(["repair", "inspect", str(setup.repair)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == info["state"]
    assert hashes(setup.repair) == before


def test_capability_gate_and_core_unchanged(tmp_path, monkeypatch):
    core = {
        t.name
        for module in importlib.import_module("sonoranlab.api.tools").MODULES
        if module is not ops
        for t in module.TOOLS
    }
    monkeypatch.setenv("SONORANLAB_EDITOR_RUNTIME", str(tmp_path / "missing"))
    assert set(registry()) == core
    assert call("capabilities", {})["result"]["editor"] == "unavailable"
    for name in OPTIONAL:
        result = call(name, {})
        assert result["error"]["code"] == "invalid_params"
        assert result["error"]["message"] == f"Unknown or disabled tool: {name}"
    runtime = tmp_path / "present"
    runtime.mkdir()
    monkeypatch.setenv("SONORANLAB_EDITOR_RUNTIME", str(runtime))
    assert set(registry()) == core | OPTIONAL
    assert call("capabilities", {})["result"]["editor"] == "available"
    assert {name for name in registry() if "repair" in name} == OPTIONAL
    monkeypatch.setenv("SONORANLAB_EDITOR_RUNTIME", str(tmp_path / "missing"))
    assert set(registry()) == core


def test_missing_editor_import_and_discovery_are_lazy(tmp_path):
    script = (
        AUDIT
        + """
from sonoranlab.api import call, registry
registry()
assert call('capabilities', {})['result']['editor'] == 'unavailable'
assert 'sonoranlab.repair.session' not in sys.modules
assert 'sonoranlab.repair.runtime' not in sys.modules
print('clean')
"""
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "SONORANLAB_EDITOR_RUNTIME": str(tmp_path / "missing"),
            "SONORANLAB_STATE": str(tmp_path / "state"),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "clean\n"
