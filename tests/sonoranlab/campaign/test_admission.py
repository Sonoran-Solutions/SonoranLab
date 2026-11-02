"""Real class-request admission (DYL-128 fixtures, 120 px cells): nothing is created unless the gates are READY."""

import importlib
import json

import pytest
import yaml
from classreq_fixtures import edit, request, write_stills
from test_pipeline import toy_profile

from sonoranlab import registry as records
from sonoranlab.api import Context, call, paths
from sonoranlab.cli import main


@pytest.fixture
def lab(tmp_path, monkeypatch):
    profile = toy_profile(tmp_path, edit)
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
    return ctx, tmp_path


def create(ctx, req, ref):
    envelope = call("campaign_create", {"project": "toy", "request": req, "reference_id": ref}, ctx=ctx)
    assert envelope["ok"], envelope
    return envelope["result"]


def campaigns(tmp):
    return list((tmp / "state/campaigns").rglob("frozen-*.json"))


def test_admission_states_create_nothing_until_ready(lab, capsys, monkeypatch):
    ctx, tmp = lab
    missing = create(ctx, request(), "refs-000000000000")
    assert (missing["created"], missing["state"]) == (False, "NEEDS_REFERENCE_PREPARATION")
    assert missing["findings"][0]["code"] == "reference_set_missing"
    ref = records.register_character("toy", "hero", str(write_stills(tmp / "stills")), ctx=ctx)["ref_set_id"]
    unapproved = create(ctx, request(), ref)
    assert (unapproved["created"], unapproved["state"]) == (False, "AWAITING_REFERENCE_APPROVAL")
    invalid = create(ctx, request(clips=["attack"]), ref)
    assert (invalid["created"], invalid["state"]) == (False, "NEEDS_OWNER_INPUT")
    assert {f["code"] for f in invalid["findings"]} == {"unsupported_clip"}
    assert not campaigns(tmp)
    # Fixture owner approval through the owner library call; an agent never runs this.
    records.approve_references("toy", "hero", ref, "Owner", "inspected eight stills", ctx=ctx)
    created = create(ctx, request(), ref)
    assert created["created"] and created["state"] == "READY" and created["budget"] == {"run": 12, "idle": 12}
    frozen = json.loads(campaigns(tmp)[0].read_text())
    assert frozen["clips"] == ["run", "idle"] and frozen["track"] == "E"
    assert frozen["references"]["ref_set_id"] == ref and len(frozen["references"]["hashes"]) == 8
    assert frozen["profile"]["character_row"]["cell"] == [120, 120]
    status = call("campaign_status", {"campaign_id": created["campaign_id"]}, ctx=ctx)["result"]
    assert status["state"] == "READY" and list(status["clips"]) == ["run", "idle"]
    # The CLI verb prints the same envelope; a non-created result exits 1.
    path = tmp / "request.json"
    path.write_text(json.dumps(request(clips=["attack"])))
    argv = ["campaign", "create", "--project", "toy", "--request", str(path), "--reference", ref]
    monkeypatch.setattr(importlib.import_module("sonoranlab.api.registry"), "Context", lambda *a, **k: ctx)
    assert main(argv) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed == call(
        "campaign_create", {"project": "toy", "request": request(clips=["attack"]), "reference_id": ref}, ctx=ctx
    )
