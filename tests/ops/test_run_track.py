"""benchmark/tools/run_track.py end to end through the real env/smoke.sh, with a fake podman (no GPU, no ComfyUI)."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FAKE = """#!/usr/bin/env bash
case "$*" in
  exec*) cat > "$FAKE_WORK/submitted.json"; mkdir -p "$FAKE_WORK/output/bench/j"
         printf x > "$FAKE_WORK/output/bench/j/out_00001_.png"
         echo '{"status": "success", "exit_code": 0, "wall_s": 1.5, "images": ["bench/j/out_00001_.png"], "errors": []}' ;;
  inspect*) echo fake ;;
esac
"""
# Same, but the exec step blocks until $FAKE_WORK/release exists (concurrency test).
FAKE_SLOW = """#!/usr/bin/env bash
case "$*" in
  exec*) cat > "$FAKE_WORK/submitted.json"; mkdir -p "$FAKE_WORK/output/bench/j"
         while [ ! -f "$FAKE_WORK/release" ]; do sleep 0.05; done
         printf x > "$FAKE_WORK/output/bench/j/out_00001_.png"
         echo '{"status": "success", "exit_code": 0, "wall_s": 1.5, "images": ["bench/j/out_00001_.png"], "errors": []}' ;;
  inspect*) echo fake ;;
esac
"""


def make_env(tmp_path, fake=FAKE):
    podman = tmp_path / "podman.sh"
    podman.write_text(fake)
    work = tmp_path / "work"
    return {**os.environ, "SONORANLAB_PODMAN": f"bash {podman}", "SONORANLAB_WORK": str(work), "FAKE_WORK": str(work),
            "SONORANLAB_UNGUARDED": "1"}, work


def make_cmd(job, ref, *extra):
    return [sys.executable, str(ROOT / "benchmark/tools/run_track.py"), "--workflow",
            str(ROOT / "workflows/pose/qwen_edit_controlnet_union_pose.json"), "--job", job, "--need-gib", "1",
            "--input", f"reference_image={ref}", *extra]


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_patches_stages_runs_and_keeps_outputs(tmp_path):
    pytest.importorskip("yaml")
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"ref")
    env, work = make_env(tmp_path)
    cmd = make_cmd("d/C/x/1", ref, "--set", "seed=7", "--set", "control_strength=0.8")
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    sent = json.loads((work / "submitted.json").read_text())
    assert sent["65"]["inputs"]["seed"] == 7 and sent["81"]["inputs"]["strength"] == 0.8
    assert sent["41"]["inputs"]["image"] == "sonoranlab/d/C/x/1/reference_image.png"
    assert (work / "input/d/C/x/1/reference_image.png").read_bytes() == b"ref"
    assert sent["9"]["inputs"]["filename_prefix"] == "bench/d/C/x/1/out"
    out = work / "benchmark/results/d/C/x/1"
    m = json.loads((out / "manifest.json").read_text())
    assert m["exit_code"] == 0 and list(m["raw"]) == ["out_00001_.png"] and m["inputs"]["reference_image"]["sha256"]
    assert m["reservation"]["job"] == "d/C/x/1" and m["reservation"]["pid"] > 0 and m["reservation"]["argv"]
    assert json.loads((out / "reservation.json").read_text()) == m["reservation"]
    again = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    assert again.returncode == 2
    assert "job d/C/x/1 is already reserved" in again.stderr


def test_concurrent_same_job_is_refused_without_touching_staged_inputs(tmp_path):
    pytest.importorskip("yaml")
    ref_a = tmp_path / "a.png"
    ref_a.write_bytes(b"A")
    ref_b = tmp_path / "b.png"
    ref_b.write_bytes(b"B")
    env, work = make_env(tmp_path, FAKE_SLOW)
    job = "d/C/x/2"
    staged = work / "input" / "d/C/x/2/reference_image.png"
    patched = work / "benchmark/results/d/C/x/2/workflow.json"

    first = subprocess.Popen(make_cmd(job, ref_a), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 30
        while not (work / "benchmark/results/d/C/x/2/reservation.json").exists():
            assert first.poll() is None, first.communicate()
            assert time.monotonic() < deadline, "reservation.json never appeared"
            time.sleep(0.02)
        before = patched.read_bytes()
        second = subprocess.run(make_cmd(job, ref_b), env=env, capture_output=True, text=True, timeout=60)
        assert second.returncode != 0
        assert "job d/C/x/2 is already reserved" in second.stderr
        assert staged.read_bytes() == b"A"
        assert patched.read_bytes() == before
        (work / "release").write_bytes(b"")
        assert first.wait(timeout=60) == 0, first.communicate()
    finally:
        if first.poll() is None:
            first.kill()
            first.wait()
    m = json.loads((work / "benchmark/results/d/C/x/2/manifest.json").read_text())
    assert m["inputs"]["reference_image"]["sha256"] == sha256(b"A")
    assert staged.read_bytes() == b"A"


def test_job_id_with_dotdot_component_is_rejected(tmp_path):
    pytest.importorskip("yaml")
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"ref")
    env, work = make_env(tmp_path)
    r = subprocess.run(make_cmd("d/C/../x/1", ref), env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2
    assert "bad job id" in r.stderr
    assert not (work / "input").exists()
    assert not (work / "benchmark").exists()
