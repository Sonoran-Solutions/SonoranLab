"""Run-window dispatch with fake exec only: never launch the guard or ComfyUI."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sonoranlab import cli
from sonoranlab.comfy import guard
from sonoranlab.comfy.adapters import track_e
from sonoranlab.comfy.adapters.base import GenerateRequest


@pytest.mark.parametrize("marker, expected", [(None, False), ("12345", True), ("54321", False), ("", False)])
def test_inside_guard_requires_direct_parent(monkeypatch, marker, expected: bool) -> None:
    monkeypatch.setattr(os, "getppid", lambda: 12345)
    monkeypatch.delenv("SONORANLAB_RUNWINDOW", raising=False)
    if marker is not None:
        monkeypatch.setenv("SONORANLAB_RUNWINDOW", marker)
    assert guard.inside_guard() is expected


def test_track_budgets_and_missing_guard(tmp_path: Path, monkeypatch) -> None:
    assert guard.min_need_gib("E") == 40 and guard.min_need_gib("C") == 60
    with pytest.raises(ValueError, match="unknown track"):
        guard.min_need_gib("A")
    monkeypatch.setattr(guard, "REPO", tmp_path)
    with pytest.raises(ValueError, match="run-window guard not found"):
        guard.guarded_argv(["fake-child"])


@pytest.fixture
def dispatch(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_exec", lambda executable, argv: calls.append((executable, argv)))
    monkeypatch.setattr(os, "getppid", lambda: 12345)
    monkeypatch.setenv("SONORANLAB_RUNWINDOW", "54321")
    monkeypatch.setenv("SONORANLAB_UNGUARDED", "1")  # inherited bypasses never authorize generation
    req = GenerateRequest("E", "samurai", "run", "east", tmp_path / "still.png", "", 7, tmp_path / "attempt")
    path = tmp_path / "request.json"
    path.write_text(json.dumps(req.to_json()))
    return SimpleNamespace(calls=calls, path=path, req=req)


@pytest.mark.parametrize("track, need", [("E", "40"), ("C", "60")])
def test_generate_enters_guard_with_original_args(dispatch, track: str, need: str, capsys) -> None:
    d = dispatch
    d.path.write_text(json.dumps({**d.req.to_json(), "track": track}))
    original = [
        "--request",
        str(d.path),
        "--exec",
        "sonoranlab-comfy",
        "--base-url",
        "http://127.0.0.1:9999",
        "--timeout",
        "42.5",
        "--allow-pause",
        "llama-gemory",
        "--allow-pause",
        "other-approved-unit",
    ]
    assert cli.main(["generate", *original]) == 0
    assert d.calls == [
        (
            "python3",
            [
                "python3",
                str(guard.REPO / "env/runwindow.py"),
                "--need-gib",
                need,
                "--allow-pause",
                "llama-gemory",
                "--allow-pause",
                "other-approved-unit",
                "--",
                sys.executable,
                "-m",
                "sonoranlab.cli",
                "generate",
                # rebuilt from the parsed options; --allow-pause goes to the guard only
                *["--request", str(d.path), "--base-url", "http://127.0.0.1:9999", "--timeout", "42.5"],
                *["--need-gib", need, "--exec", "sonoranlab-comfy"],
            ],
        )
    ]
    assert "--gtt-credit" not in d.calls[0][1]
    assert capsys.readouterr().err == f"generate: entering env/runwindow.py --need-gib {need}\n"
    assert not d.req.out_dir.exists()


@pytest.mark.parametrize("track, need", [("E", "30"), ("C", "40"), ("E", "0"), ("E", "nan"), ("E", "inf")])
def test_generate_refuses_under_budget(dispatch, track: str, need: str, capsys) -> None:
    d = dispatch
    d.path.write_text(json.dumps({**d.req.to_json(), "track": track}))
    assert cli.main(["generate", "--request", str(d.path), "--need-gib", need]) == 2
    assert f"refusing an under-budget run window; Track {track} needs {guard.min_need_gib(track):g} GiB" in (
        capsys.readouterr().err
    )
    assert d.calls == []


def test_generate_accepts_larger_budget(dispatch) -> None:
    assert cli.main(["generate", "--request", str(dispatch.path), "--need-gib", "64.5"]) == 0
    assert dispatch.calls[0][1][2:4] == ["--need-gib", "64.5"]


@pytest.mark.parametrize("contents", ["{bad json", '{"track": "E"}', '{"track": "A"}'])
def test_bad_request_never_execs(dispatch, contents: str) -> None:
    dispatch.path.write_text(contents)
    assert cli.main(["generate", "--request", str(dispatch.path)]) == 2
    assert dispatch.calls == []


def test_generate_inside_guard_runs_adapter(dispatch, monkeypatch) -> None:
    monkeypatch.setenv("SONORANLAB_RUNWINDOW", "12345")
    generated = []

    def generate(self, req):
        generated.append(req)
        return SimpleNamespace(status="ok", to_json=lambda: {"status": "ok"})

    monkeypatch.setattr(track_e, "make_client", lambda *a, **kw: object())
    monkeypatch.setattr(track_e.TrackE, "generate", generate)
    assert cli.main(["generate", "--request", str(dispatch.path)]) == 0
    assert generated == [dispatch.req] and dispatch.calls == []
