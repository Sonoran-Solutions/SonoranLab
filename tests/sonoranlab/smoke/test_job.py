"""MCP `smoke_render` job: registry/STATE refs only, frozen inputs, fake-engine run, staged game sheets."""

import json
import re
from types import SimpleNamespace

import pytest
import synth

from sonoranlab import registry, smoke
from sonoranlab.api import call, paths
from sonoranlab.api.registry import Context
from sonoranlab.api.tools import deterministic as ops
from sonoranlab.jobs import manager, store, worker
from sonoranlab.jobs.kinds import smoke as kind

BASE = {"project": "toy", "character": "hero", "siblings": []}


@pytest.fixture
def api(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "profiles").mkdir(parents=True)
    profile = synth.toy_profile(repo / "profiles")
    ctx = Context(repo)
    monkeypatch.setenv("SONORANLAB_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", str(tmp_path))
    for module in (paths, kind, ops):
        monkeypatch.setattr(module, "Context", lambda: ctx)
    work = tmp_path / "work"
    work.mkdir()
    return SimpleNamespace(root=tmp_path, ctx=ctx, work=work, profile=profile, state=tmp_path / "state")


def refs(api, monkeypatch, **dirs):
    """Name export dirs as refs, as `_reference` would for imported artifacts."""
    monkeypatch.setattr(ops, "_reference", lambda value, ctx: (dirs[value], value))


def execute(params):
    patch = pytest.MonkeyPatch()
    patch.setattr(manager, "launch", lambda job: None)
    try:
        submitted = manager.submit("smoke_render", params)
    finally:
        patch.undo()
    worker._work(store.job_dir(submitted["job_id"]))
    return manager.get(submitted["job_id"])


def test_tool_is_a_typed_job_with_closed_schema(api):
    from sonoranlab.api.registry import registry as tools

    tool = tools(api.ctx)["smoke_render"]
    assert tool.kind == "job" and not tool.read_only
    schema = tool.input_schema
    assert schema["additionalProperties"] is False and schema["properties"]["siblings"]["maxItems"] == 3
    assert set(schema["properties"]) == {"project", "character", "candidate", "siblings", "idempotency_key"}
    for extra in ({"scene": "x"}, {"script": "x"}, {"shader": "/etc/passwd"}, {"out": "/tmp/x"}):
        bad = call("smoke_render", BASE | {"candidate": "job-20260101T000000Z-deadbeef"} | extra, ctx=api.ctx)
        assert not bad["ok"] and bad["error"]["code"] == "invalid_params"
    four = call("smoke_render", BASE | {"candidate": "x", "siblings": list("abcd")}, ctx=api.ctx)
    assert not four["ok"]


@pytest.mark.parametrize(
    "candidate",
    ["job-20260101T000000Z-deadbeef", "import-0123456789abcdef01234567", "/etc/passwd", "../../etc", "frames", "x:y"],
)
def test_unknown_or_unregistered_refs_refused(api, candidate):
    result = call("smoke_render", BASE | {"candidate": candidate}, ctx=api.ctx)
    assert not result["ok"] and result["error"]["code"] in {"not_found", "invalid_params", "path_refused", "refused"}


def test_a_real_path_is_never_a_candidate_or_sibling(api):
    job = synth.export_job(api.work, "hero")
    other = synth.export_job(api.work, "ally")
    for params in ({"candidate": str(job)}, {"candidate": "unused", "siblings": [str(other)]}):
        result = call("smoke_render", BASE | params, ctx=api.ctx)
        assert not result["ok"], result


def test_unregistered_character_refused(api):
    result = call(
        "smoke_render", BASE | {"character": "nobody", "candidate": "job-20260101T000000Z-deadbeef"}, ctx=api.ctx
    )
    assert not result["ok"] and result["error"]["field"] == "character"


def test_job_runs_with_fake_engine_and_freezes_inputs(api, monkeypatch):
    hero, ally = synth.export_job(api.work, "hero"), synth.export_job(api.work, "ally")
    refs(api, monkeypatch, cand=hero, sib=ally)
    synth.patch_engine(monkeypatch)
    params = BASE | {"candidate": "cand", "siblings": ["sib"]}
    job = execute(params)
    assert job["state"] == "succeeded", job
    result = job["result"]
    assert result["technical"] == {"validator": None, "godot": "pass"}
    assert result["agent_recommendation"] == "" and result["owner_style_acceptance"] == ""
    assert result["smoke"]["border"] == "not_rendered" and result["renders"] == 2 + 8 * 3 * 2
    doc = json.loads((store.output(job) / "smoke/smoke.json").read_text())
    assert doc["status"] == "pass" and [s["label"] for s in doc["inputs"]["siblings"]] == ["ally"]
    assert not (store.output(job) / "review.json").exists()
    assert re.fullmatch(r"job-[0-9TZ]+-[0-9a-f]{8}:smoke/smoke.json", result["ref"])


def test_inputs_changed_after_admission_are_refused(api, monkeypatch):
    hero = synth.export_job(api.work, "hero")
    refs(api, monkeypatch, cand=hero)
    patch = pytest.MonkeyPatch()
    patch.setattr(manager, "launch", lambda job: None)
    try:
        submitted = manager.submit("smoke_render", BASE | {"candidate": "cand"})
    finally:
        patch.undo()
    png = hero / "export/hero/hero.png"
    png.write_bytes(png.read_bytes() + b"\0")
    worker._work(store.job_dir(submitted["job_id"]))
    assert manager.get(submitted["job_id"])["state"] == "failed"


def test_missing_engine_is_unavailable_not_pass(api, monkeypatch):
    refs(api, monkeypatch, cand=synth.export_job(api.work, "hero"))
    monkeypatch.setattr(smoke, "find_engine", lambda godot=None: (None, "no godot"))
    job = execute(BASE | {"candidate": "cand"})
    assert job["state"] == "succeeded" and job["result"]["technical"]["godot"] == "unavailable"
    assert job["result"]["smoke"]["status"] == "unavailable"


def test_candidate_must_be_the_requested_character_and_siblings_distinct(api, monkeypatch):
    refs(api, monkeypatch, cand=synth.export_job(api.work, "ally"), same=synth.export_job(api.work, "hero"))
    with pytest.raises(Exception, match="expected exactly one export sidecar"):
        manager.submit("smoke_render", BASE | {"candidate": "cand"})
    refs(api, monkeypatch, cand=synth.export_job(api.work, "hero"), a=synth.export_job(api.work, "hero"))
    with pytest.raises(Exception, match="repeats a character"):
        manager.submit("smoke_render", BASE | {"candidate": "cand", "siblings": ["a"]})


def test_staged_shader_under_state_is_used_and_a_symlink_refused(api, monkeypatch):
    refs(api, monkeypatch, cand=synth.export_job(api.work, "hero"))
    synth.patch_engine(monkeypatch)
    shader = api.state / "smoke/PixelStandee.gdshader"
    shader.parent.mkdir(parents=True)
    shader.write_text(synth.STAND_SHADER)
    job = execute(BASE | {"candidate": "cand"})
    assert job["result"]["smoke"]["border"] == "rendered"
    shader.unlink()
    shader.symlink_to(api.root / "elsewhere")
    with pytest.raises(Exception, match="symlink"):
        manager.submit("smoke_render", BASE | {"candidate": "cand"})


def test_staged_game_sheet_imports_and_resolves_as_sibling(api, monkeypatch):
    """The owner's recipe (docs/09): stage the game sheet, import_job it, name the artifact id as a sibling."""
    staged = synth.game_sheet(api.work, "ally")
    job = smoke.stage_game_sheet(api.profile, staged.parent / "characters.json", "ally", api.work / "ally-staged")
    imported = registry.import_job("toy", "ally", str(job), ctx=api.ctx)
    hero = synth.export_job(api.work, "hero")
    real = ops._reference
    monkeypatch.setattr(ops, "_reference", lambda v, c: (hero, v) if v == "cand" else real(v, c))
    synth.patch_engine(monkeypatch)
    run = execute(BASE | {"candidate": "cand", "siblings": [imported["artifact_id"]]})
    assert run["state"] == "succeeded", run
    doc = json.loads((store.output(run) / "smoke/smoke.json").read_text())
    assert doc["inputs"]["siblings"][0]["label"] == "ally" and doc["status"] == "pass"
    with pytest.raises(smoke.SmokeError, match="exists"):
        smoke.stage_game_sheet(api.profile, staged.parent / "characters.json", "ally", job)
