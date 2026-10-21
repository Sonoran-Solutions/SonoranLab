"""Run-window dispatch with fake exec only: never launch the guard or ComfyUI."""

import fcntl
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sonoranlab import cli
from sonoranlab.comfy import guard
from sonoranlab.comfy.adapters import track_c, track_e
from sonoranlab.comfy.adapters.base import GenerateRequest


@pytest.mark.parametrize("marker, expected", [(None, False), ("12345", True), ("54321", False), ("", False)])
def test_inside_guard_requires_direct_parent(lease_window, monkeypatch, marker, expected: bool) -> None:
    lease_window()
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
    monkeypatch.delenv("SONORANLAB_RUNWINDOW", raising=False)
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


@pytest.mark.parametrize("track,budget", [("C", 60), ("E", 40)])
def test_generate_inside_guard_runs_adapter(dispatch, lease_window, monkeypatch, track, budget) -> None:
    window = lease_window(need_gib=budget)
    assert guard.admitted_window() == window.data
    assert guard.inside_guard(budget) and not guard.inside_guard(budget + 1)
    dispatch.path.write_text(json.dumps({**dispatch.req.to_json(), "track": track}))
    req = GenerateRequest.from_json(json.loads(dispatch.path.read_text()))
    generated = []

    def generate(self, req):
        generated.append(req)
        return SimpleNamespace(status="ok", to_json=lambda: {"status": "ok"})

    module, adapter = (track_c, track_c.TrackC) if track == "C" else (track_e, track_e.TrackE)
    monkeypatch.setattr(module, "make_client", lambda *a, **kw: object())
    monkeypatch.setattr(adapter, "generate", generate)
    assert cli.main(["generate", "--request", str(dispatch.path)]) == 0
    assert generated == [req] and dispatch.calls == []


@pytest.mark.parametrize(
    "fault",
    [
        "undersized",
        "nonce",
        "missing_nonce",
        "parent",
        "pid",
        "unlocked",
        "boot",
        "corrupt",
        "missing",
        "schema",
        "bool_budget",
        "nan_budget",
        "infinite_budget",
        "non_numeric_budget",
        "owner",
        "directory",
        "fifo",
    ],
)
def test_generate_refuses_unverifiable_window_before_staging(dispatch, lease_window, monkeypatch, capsys, fault):
    window = lease_window(need_gib=40 if fault == "undersized" else 60)
    dispatch.path.write_text(json.dumps({**dispatch.req.to_json(), "track": "C"}))
    if fault == "nonce":
        monkeypatch.setenv("SONORANLAB_RUNWINDOW_NONCE", "wrong")
    elif fault == "missing_nonce":
        monkeypatch.delenv("SONORANLAB_RUNWINDOW_NONCE")
    elif fault == "parent":
        monkeypatch.setenv("SONORANLAB_RUNWINDOW", "54321")
    elif fault == "pid":
        window.data["pid"] = 54321
    elif fault == "unlocked":
        fcntl.flock(window.lock, fcntl.LOCK_UN)
    elif fault == "boot":
        window.boot.write_text("another-boot")
    elif fault == "schema":
        window.data["schema"] = "unknown"
    elif fault.endswith("_budget"):
        window.data["need_gib"] = {
            "bool_budget": True,
            "nan_budget": float("nan"),
            "infinite_budget": float("inf"),
            "non_numeric_budget": "60",
        }[fault]
    elif fault == "owner":
        uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    window.path.write_text(json.dumps(window.data))
    if fault == "corrupt":
        window.path.write_text("{broken json")
    elif fault in ("missing", "directory", "fifo"):
        window.path.unlink()
        if fault == "directory":
            window.path.mkdir()
        elif fault == "fifo":
            os.mkfifo(window.path)
    assert cli.main(["generate", "--request", str(dispatch.path)]) == 3
    assert "REFUSED" in capsys.readouterr().err
    assert not dispatch.calls and not dispatch.req.out_dir.exists()


def test_generate_checks_explicit_need_against_lease(dispatch, lease_window, capsys):
    lease_window(need_gib=60)
    assert cli.main(["generate", "--request", str(dispatch.path), "--need-gib", "64"]) == 3
    assert "requires 64 GiB" in capsys.readouterr().err
    assert not dispatch.calls and not dispatch.req.out_dir.exists()


def test_generate_refuses_empty_window_claim(dispatch, monkeypatch):
    monkeypatch.setenv("SONORANLAB_RUNWINDOW", "")
    assert cli.main(["generate", "--request", str(dispatch.path)]) == 3
    assert not dispatch.calls and not dispatch.req.out_dir.exists()


def test_stale_lock_probe_is_released(lease_window):
    window = lease_window()
    fcntl.flock(window.lock, fcntl.LOCK_UN)
    assert guard.admitted_window() is None
    fcntl.flock(window.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
