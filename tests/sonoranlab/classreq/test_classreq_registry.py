"""Changed references after approval (registry binds approval to hashes) and CLI/MCP parity of check_class_request."""

import json

import pytest
import yaml
from classreq_fixtures import request, still, write_stills

from sonoranlab import registry as records
from sonoranlab.api import Context, call, paths
from sonoranlab.cli import main


@pytest.fixture
def lab(tmp_path, profile, monkeypatch):
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
    return ctx, repo / "profiles/toy.yaml", tmp_path


def test_changed_references_are_a_new_unapproved_registration(lab):
    ctx, _, tmp = lab
    a = write_stills(tmp / "set_a")
    ref_a = records.register_character("toy", "hero", str(a), notes="class request", ctx=ctx)["ref_set_id"]
    records.approve_references("toy", "hero", ref_a, "Owner", "inspected eight stills", ctx=ctx)
    changed = still("south")
    changed[100, 60] = (40, 60, 200, 255)
    b = write_stills(tmp / "set_b", {"south": changed})
    result_b = records.register_character("toy", "hero", str(b), ctx=ctx)
    assert result_b["created"] and result_b["ref_set_id"] != ref_a
    assert result_b["approval"]["state"] == "unapproved"
    sets = {
        s["ref_set_id"]: s["approval"]["state"] for s in records.get_character("toy", "hero", ctx=ctx)["reference_sets"]
    }
    assert sets == {ref_a: "approved", result_b["ref_set_id"]: "unapproved"}


def test_cli_mcp_parity(lab, capsys):
    ctx, profile, tmp = lab
    ref = records.register_character("toy", "hero", str(write_stills(tmp / "stills")), ctx=ctx)["ref_set_id"]
    stored = tmp / "state/registry/toy/hero/refs" / ref
    cases = [
        (request(), ref),
        (request(character="rogue"), None),
        (request(clips=["attack"]), ref),
        (request(silhouette="\x1b[31m"), None),
    ]
    for i, (req, stills_ref) in enumerate(cases):
        path = tmp / f"req{i}.json"
        path.write_text(json.dumps(req))
        argv = ["classreq", "check", str(path), "--profile", str(profile), "--state", str(tmp / "state")]
        if stills_ref:
            argv += ["--stills", str(stored)]
        code = main(argv)
        cli = json.loads(capsys.readouterr().out)
        params = {"project": "toy", "request": req} | ({"stills_ref": stills_ref} if stills_ref else {})
        envelope = call("check_class_request", params, ctx=ctx)
        assert envelope["ok"], envelope
        api = envelope["result"]
        for k in ("status", "findings", "request_sha256", "cell", "foot_y", "profile_row", "proposed_row", "registry"):
            assert cli[k] == api[k], (i, k)
        assert cli["stills"]["files"] == api["stills"]["files"]
        assert code == (0 if api["status"] == "ready" else 1)
    assert [
        call("check_class_request", {"project": "toy", "request": r}, ctx=ctx)["result"]["status"] for r, _ in cases
    ] == [
        "needs_reference_preparation",
        "needs_reference_preparation",
        "invalid",
        "invalid",
    ]
    ready = call("check_class_request", {"project": "toy", "request": request(), "stills_ref": ref}, ctx=ctx)["result"]
    assert ready["status"] == "ready" and ready["registry"]["matching_ref_set"] == ref


def test_mcp_refuses_bad_refs_and_ignores_request_paths(lab):
    ctx, _, tmp = lab
    write_stills(tmp / "stills")
    req = request(references={"stills_dir": str(tmp / "stills"), "provenance": "x", "approved_by": None})
    result = call("check_class_request", {"project": "toy", "request": req}, ctx=ctx)["result"]
    assert {f["code"] for f in result["findings"]} == {"missing_still"}  # the request's path is never followed
    bad = call("check_class_request", {"project": "toy", "request": req, "stills_ref": "../x"}, ctx=ctx)
    assert bad["error"]["code"] == "invalid_params"
    missing = call("check_class_request", {"project": "toy", "request": req, "stills_ref": "refs-" + "0" * 12}, ctx=ctx)
    assert missing["error"]["code"] == "not_found"
    assert call("check_class_request", {"project": "nope", "request": req}, ctx=ctx)["error"]["code"] == "not_found"
    assert not (tmp / "state").exists() or not any((tmp / "state").rglob("*.json"))


def test_tool_is_read_only_and_listed():
    from sonoranlab.api import registry

    tool = registry()["check_class_request"]
    assert tool.read_only and tool.kind == "sync"
