"""Phase 1 job runner (disposable benchmark tooling, not the Phase 2 package).

Patches a workflow's named params, stages input images where LoadImage can see them, runs it through the guarded
env/smoke.sh (run-window preflight, queue drain, memory sampling), then copies the outputs into the job's raw/ folder
next to a manifest. Every attempt keeps its manifest, failures included. Nothing is written into this public repo.

    uv run --no-project --with pyyaml python -I benchmark/tools/run_track.py \
      --workflow workflows/pose/qwen_edit_controlnet_union_pose.json --job 2026-10-18/C/samurai/1/east_0 \
      --need-gib 56 --input reference_image=~/sonoranlab-work/benchmark/characters/2026-10-18/samurai/still_south_x4.png \
      --input control_image=.../openpose_east_0.png --set seed=1 --set width=736 --set height=736

Inputs go to $WORK/input/<job>/ (mounted read-only at /opt/ComfyUI/input/sonoranlab by env/comfy.sh); outputs land
in $WORK/output/bench/<job>/ and are copied to $WORK/benchmark/results/<job>/raw/. Exit code = smoke.sh's.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORK = Path(os.environ.get("SONORANLAB_WORK", Path.home() / "sonoranlab-work"))


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def targets(spec) -> list[dict]:
    return spec if isinstance(spec, list) else [spec]


def put(wf: dict, params: dict, key: str, value) -> None:
    if key not in params:
        raise SystemExit(f"unknown param {key!r}; known: {sorted(params)}")
    for t in targets(params[key]):
        wf[t["node"]]["inputs"][t["input"]] = value


def value(s: str):
    try:
        return json.loads(s)
    except ValueError:
        return s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workflow", required=True, type=Path)
    ap.add_argument("--job", required=True, help="<date>/<track>/<char>/<seed>[/<part>]")
    ap.add_argument("--need-gib", type=float, required=True)
    ap.add_argument("--allow-pause", action="append", default=[], help="owner-approved user unit (per run)")
    ap.add_argument("--input", action="append", default=[], metavar="PARAM=PATH")
    ap.add_argument("--set", action="append", default=[], metavar="PARAM=VALUE", help="JSON value or plain string")
    ap.add_argument("--expect-images", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--settle-s", type=int, help="post-pause settle wait for the run window (env/smoke.sh SMOKE_SETTLE_S)")
    a = ap.parse_args(argv)

    wf_path = a.workflow.resolve()
    wf = json.loads(wf_path.read_text())
    params = yaml.safe_load(wf_path.with_suffix(".params.yaml").read_text())
    slug = a.job.strip("/").replace("/", "_")
    out = WORK / "benchmark" / "results" / a.job
    if (out / "manifest.json").exists():
        raise SystemExit(f"{out} already has a manifest; raw outputs are never overwritten (pick a new job id)")
    inp = WORK / "input" / slug
    inp.mkdir(parents=True, exist_ok=True)
    (out / "raw").mkdir(parents=True, exist_ok=True)

    inputs = {}
    for kv in a.input:
        key, path = kv.split("=", 1)
        src = Path(path).expanduser().resolve()
        dst = inp / f"{key}{src.suffix}"
        shutil.copyfile(src, dst)
        put(wf, params, key, f"sonoranlab/{slug}/{dst.name}")
        inputs[key] = {"source": str(src), "sha256": sha(dst)}
    sets = {}
    for kv in a.set:
        key, raw = kv.split("=", 1)
        sets[key] = value(raw)
        put(wf, params, key, sets[key])
    for key in params:
        if key.startswith("output_prefix"):
            put(wf, params, key, f"bench/{slug}/{key.removeprefix('output_prefix').strip('_') or 'out'}")

    patched = out / "workflow.json"
    patched.write_text(json.dumps(wf, indent=2) + "\n")
    env = {**os.environ, "SMOKE_NEED_GIB": str(a.need_gib), "SMOKE_ALLOW_PAUSE": " ".join(a.allow_pause),
           "SMOKE_EXPECT_IMAGES": str(a.expect_images), "SMOKE_TIMEOUT": str(a.timeout),
           **({"SMOKE_SETTLE_S": str(a.settle_s)} if a.settle_s else {})}
    rc = subprocess.run(["bash", str(REPO / "env" / "smoke.sh"), str(patched), slug], env=env).returncode

    run_manifest = WORK / "runs" / f"{slug}.json"
    run = json.loads(run_manifest.read_text()) if run_manifest.exists() else {"status": "no-manifest", "exit_code": rc}
    raw = {}
    for image in run.get("images", []):
        src = WORK / "output" / image
        if src.is_file():
            dst = out / "raw" / Path(image).name
            shutil.copyfile(src, dst)
            raw[dst.name] = sha(dst)
    rev = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    manifest = {"job": a.job, "workflow_source": str(wf_path.relative_to(REPO)) if wf_path.is_relative_to(REPO) else str(wf_path), "workflow_source_sha256": sha(wf_path),
                "workflow_patched_sha256": sha(patched), "repo_rev": rev, "inputs": inputs, "set": sets,
                "need_gib": a.need_gib, "allow_pause": a.allow_pause, "exit_code": rc, "run": run, "raw": raw}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({k: manifest[k] for k in ("job", "exit_code", "raw")} | {"wall_s": run.get("wall_s")}, indent=1))
    return rc


if __name__ == "__main__":
    sys.exit(main())
