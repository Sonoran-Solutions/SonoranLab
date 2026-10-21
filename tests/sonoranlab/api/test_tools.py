import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from sonoranlab.api import call, registry
from sonoranlab.api.registry import ENVELOPE_SCHEMA
from sonoranlab.api.tools import system
from sonoranlab.profile import load_profile

REPO = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("available", [False, True])
def test_capabilities_presence_only(monkeypatch, tmp_path, available):
    runtime = tmp_path / "runtime"
    if available:
        runtime.mkdir()
    monkeypatch.setenv("SONORANLAB_EDITOR_RUNTIME", str(runtime))
    monkeypatch.setenv("SONORANLAB_COMFY_CONTAINER", "test-comfy")
    monkeypatch.setattr(system.shutil, "which", lambda name: "/test/godot")

    def forbidden(*args, **kwargs):
        pytest.fail("read tool launched a subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    result = call("capabilities", {})
    Draft202012Validator(ENVELOPE_SCHEMA).validate(result)
    data = result["result"]
    assert data["comfyui"] == {"container": "test-comfy", "status": "not_probed"}
    assert data["godot"] == {"path": "/test/godot"}
    assert data["editor"] == ("available" if available else "unavailable")
    assert data["api_schema"] == "sonoranlab.api/1" and data["server_version"]
    assert data["tools"] == [tool.summary() for tool in registry().values()]
    assert "cloud fallback" in data["offline_policy"]
    assert not (tmp_path / "state").exists()


def test_missing_godot(monkeypatch):
    monkeypatch.setattr(system.shutil, "which", lambda name: None)
    assert call("capabilities", {})["result"]["godot"] == {"path": None}


def test_projects_and_resolved_profile_without_private_reads(monkeypatch):
    original = Path.open

    def confined_open(path, *args, **kwargs):
        assert path.resolve().is_relative_to(REPO), f"Read outside repo: {path}"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", confined_open)
    projects = call("list_projects", {})["result"]["projects"]
    assert projects == [{"id": "twos-are-up", "name": "Twos Are Up", "profile_path": "profiles/twos-are-up.yaml"}]
    result = call("get_profile", {"project": "twos-are-up"})
    Draft202012Validator(ENVELOPE_SCHEMA).validate(result)
    data = result["result"]
    profile = load_profile(REPO / projects[0]["profile_path"])
    assert data["cells_per_character"] == {name: list(profile.character(name).cell) for name in profile.characters}
    assert len(data["directions"]["order"]) == 8 and len(data["directions"]["generate"]) == 5
    assert len(data["directions"]["mirror_animations"]) == 3
    assert data["animations"]["run"]["frames"] == profile.animations["run"].frames
    assert data["validator"]["defaults"]["max_bbox_drift_px"] == 25
    assert data["autonomy"]["allow_auto_approve"] is False
    assert data["export"]["read_only"] and data["export"]["protected"]
    assert data["palette"] == {"path": str(profile.palette.source), "sha256": profile.palette.sha256}
    assert "#" not in json.dumps(data)


@pytest.mark.parametrize(
    "params", [{}, {"project": "../bad"}, {"project": 2}, {"project": "twos-are-up", "override": {}}]
)
def test_profile_schema_errors(params):
    assert call("get_profile", params)["error"]["code"] == "invalid_params"


def test_missing_project():
    assert call("get_profile", {"project": "missing"})["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    ("args", "stdin", "code"),
    [
        (["api", "capabilities"], None, 0),
        (["api", "get_profile", "--params", "-"], '{"project":"twos-are-up"}', 0),
        (["api", "get_profile"], None, 2),
        (["api", "get_profile", "--params", "-"], '{"project":"missing"}', 1),
        (["api", "missing"], None, 2),
        (["api"], None, 2),
        (["api", "--list", "capabilities"], None, 2),
        (["api", "capabilities", "--params", "-"], "{", 2),
        (["api", "capabilities", "--params", "-"], '{"value":NaN}', 2),
        (["api", "capabilities", "--params", "/nonexistent-dyl113.json"], None, 2),
    ],
)
def test_cli_envelope_and_exit(args, stdin, code):
    proc = subprocess.run(
        [sys.executable, "-m", "sonoranlab.cli", *args],
        input=stdin,
        text=True,
        capture_output=True,
        env=os.environ,
        timeout=10,
    )
    assert proc.returncode == code, proc.stderr
    Draft202012Validator(ENVELOPE_SCHEMA).validate(json.loads(proc.stdout))
    assert "Traceback" not in proc.stdout


def test_cli_list_and_file_params(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "sonoranlab.cli", "api", "--list"],
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert json.loads(proc.stdout) == [tool.summary() for tool in registry().values()]
    params = tmp_path / "params.json"
    params.write_text('{"project":"twos-are-up"}')
    proc = subprocess.run(
        [sys.executable, "-m", "sonoranlab.cli", "api", "get_profile", "--params", str(params)],
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert json.loads(proc.stdout) == call("get_profile", {"project": "twos-are-up"})
