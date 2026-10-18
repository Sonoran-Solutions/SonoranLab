"""benchmark/tools/run_track.py end to end through the real env/smoke.sh, with a fake podman (no GPU, no ComfyUI)."""

import json
import os
import subprocess
import sys
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


def test_patches_stages_runs_and_keeps_outputs(tmp_path):
    pytest.importorskip("yaml")
    podman = tmp_path / "podman.sh"
    podman.write_text(FAKE)
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"ref")
    work = tmp_path / "work"
    env = {**os.environ, "SONORANLAB_PODMAN": f"bash {podman}", "SONORANLAB_WORK": str(work), "FAKE_WORK": str(work),
           "SONORANLAB_UNGUARDED": "1"}
    cmd = [sys.executable, str(ROOT / "benchmark/tools/run_track.py"), "--workflow",
           str(ROOT / "workflows/pose/qwen_edit_controlnet_union_pose.json"), "--job", "d/C/x/1", "--need-gib", "1",
           "--input", f"reference_image={ref}", "--set", "seed=7", "--set", "control_strength=0.8"]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    sent = json.loads((work / "submitted.json").read_text())
    assert sent["65"]["inputs"]["seed"] == 7 and sent["81"]["inputs"]["strength"] == 0.8
    assert sent["41"]["inputs"]["image"] == "sonoranlab/d_C_x_1/reference_image.png"
    assert (work / "input/d_C_x_1/reference_image.png").read_bytes() == b"ref"
    assert sent["9"]["inputs"]["filename_prefix"] == "bench/d_C_x_1/out"
    m = json.loads((work / "benchmark/results/d/C/x/1/manifest.json").read_text())
    assert m["exit_code"] == 0 and list(m["raw"]) == ["out_00001_.png"] and m["inputs"]["reference_image"]["sha256"]
    again = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    assert again.returncode != 0 and "never overwritten" in again.stderr
