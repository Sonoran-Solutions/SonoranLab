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
# Same, but the exec step logs each invocation, writes $FAKE_WORK/dispatched once the whole workflow arrived on stdin
# (the runner finished staging and writing workflow.json first), then blocks until $FAKE_WORK/release exists.
FAKE_SLOW = """#!/usr/bin/env bash
case "$*" in
  exec*) echo x >> "$FAKE_WORK/exec.log"; cat > "$FAKE_WORK/submitted.json"; mkdir -p "$FAKE_WORK/output/bench/j"
         : > "$FAKE_WORK/dispatched"
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
    return {
        **os.environ,
        "SONORANLAB_PODMAN": f"bash {podman}",
        "SONORANLAB_WORK": str(work),
        "FAKE_WORK": str(work),
        "SONORANLAB_UNGUARDED": "1",
    }, work


def make_cmd(job, ref, *extra):
    return [
        sys.executable,
        str(ROOT / "benchmark/tools/run_track.py"),
        "--workflow",
        str(ROOT / "workflows/pose/qwen_edit_controlnet_union_pose.json"),
        "--job",
        job,
        "--need-gib",
        "1",
        "--input",
        f"reference_image={ref}",
        *extra,
    ]


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


def _wait_for(first, path, what, timeout=30):
    """Bounded wait for `path`; on failure report the child's status and stderr (the child is reaped in the caller)."""
    deadline = time.monotonic() + timeout
    while not path.exists():
        if first.poll() is not None or time.monotonic() > deadline:
            if first.poll() is None:
                first.kill()
            out, err = first.communicate(timeout=10)
            pytest.fail(f"{what} never appeared (returncode={first.returncode}); stdout={out!r} stderr={err!r}")
        time.sleep(0.02)


def concurrent_protocol(tmp_path, *, delay_s="0", unsafe=False, wait_for_dispatch=True):
    ref_a = tmp_path / "a.png"
    ref_a.write_bytes(b"A")
    ref_b = tmp_path / "b.png"
    ref_b.write_bytes(b"B")
    env, work = make_env(tmp_path, FAKE_SLOW)
    env["RT_STAGE_DELAY_S"] = delay_s
    if unsafe:
        env["RT_UNSAFE"] = "1"
    job = "d/C/x/2"
    res = work / "benchmark/results/d/C/x/2"
    staged = work / "input" / "d/C/x/2/reference_image.png"
    patched = res / "workflow.json"

    def cmd(ref):
        return [sys.executable, str(Path(__file__).with_name("run_track_variant.py")), *make_cmd(job, ref)[2:]]

    first = subprocess.Popen(cmd(ref_a), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # The runner reserves first and stages/writes workflow.json afterwards, so reservation.json proves nothing about
        # the inputs; the fake's `dispatched` marker appears only after it received the complete workflow on stdin.
        _wait_for(
            first,
            work / ("dispatched" if wait_for_dispatch else "benchmark/results/d/C/x/2/reservation.json"),
            "dispatch marker" if wait_for_dispatch else "reservation.json",
        )
        before = patched.read_bytes()
        try:
            second = subprocess.run(cmd(ref_b), env=env, capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            raise AssertionError("second submission was accepted and is running instead of being refused") from None
        assert second.returncode != 0
        assert "job d/C/x/2 is already reserved" in second.stderr, second.stderr
        assert staged.read_bytes() == b"A"
        assert patched.read_bytes() == before
        assert (work / "exec.log").read_text().count("x") == 1, "second submission reached the GPU/job dispatch"
        (work / "release").write_bytes(b"")
        out, err = first.communicate(timeout=60)
        assert first.returncode == 0, (first.returncode, out, err)
    finally:
        (work / "release").write_bytes(b"")  # never leave a fake exec spinning
        if first.poll() is None:
            first.kill()
        first.communicate()
    m = json.loads((res / "manifest.json").read_text())
    assert m["inputs"]["reference_image"]["sha256"] == sha256(b"A")
    assert staged.read_bytes() == b"A"


@pytest.mark.parametrize("delay_s", ["0", "0.6"])
def test_concurrent_same_job_is_refused_without_touching_staged_inputs(tmp_path, delay_s):
    pytest.importorskip("yaml")
    concurrent_protocol(tmp_path, delay_s=delay_s)


def test_old_reservation_only_wait_races_with_staging(tmp_path):
    """The previous synchronization (wait for reservation.json, then read workflow.json) fails when staging is slow."""
    pytest.importorskip("yaml")
    with pytest.raises(FileNotFoundError):
        concurrent_protocol(tmp_path, delay_s="0.6", wait_for_dispatch=False)


def test_concurrent_protocol_catches_a_runner_with_non_exclusive_reservation(tmp_path):
    pytest.importorskip("yaml")
    with pytest.raises(AssertionError):
        concurrent_protocol(tmp_path, unsafe=True)


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
