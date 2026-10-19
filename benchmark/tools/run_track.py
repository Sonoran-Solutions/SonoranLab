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

A job id is used exactly once. Before staging anything the runner atomically reserves $WORK/benchmark/results/<job>/
(mkdir without exist_ok, then an exclusive reservation.json), so two launches with the same id can never interleave
their inputs. An interrupted attempt keeps its directory and therefore stays reserved; recovery is a new job id (the
sweep scripts already retry as <id>-r1, -r2, ...). Exit code 2 on a reservation or job-id validation failure.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import socket
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


COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")


def refuse(msg: str) -> SystemExit:
    """Exit code 2 with the message on stderr (see env/smoke.sh's exit-code table)."""
    print(msg, file=sys.stderr)
    return SystemExit(2)


def job_parts(job: str) -> list[str]:
    """Validate a job id: slash-separated components, each a safe single path segment."""
    parts = job.strip("/").split("/")
    for part in parts:
        if part in ("", ".", "..") or not COMPONENT.match(part):
            raise refuse(f"bad job id {job!r}: component {part!r} must match ^[A-Za-z0-9._-]+$ and not be . or ..")
    return parts


def reserve(out: Path, job: str, argv: list[str]) -> dict:
    """Claim the job's result directory before any other write; a kept directory stays reserved."""
    taken = f"job {job} is already reserved (in progress, interrupted, or done); use a new job id"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        out.mkdir()
    except FileExistsError:
        raise refuse(taken) from None
    reservation = {"job": job, "pid": os.getpid(), "host": socket.gethostname(),
                   "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "argv": argv}
    try:
        with open(out / "reservation.json", "x") as f:
            f.write(json.dumps(reservation, indent=2) + "\n")
    except FileExistsError:
        raise refuse(taken) from None
    return reservation


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
    job = "/".join(job_parts(a.job))
    # smoke.sh run tag ($WORK/runs/<tag>.json, read back below): a flat name, made collision-free by the job's hash
    slug = f"{job.replace('/', '_')}-{hashlib.sha256(job.encode()).hexdigest()[:12]}"
    out = WORK / "benchmark" / "results" / Path(job)
    reservation = reserve(out, job, sys.argv)
    inp = WORK / "input" / Path(job)
    try:
        inp.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise refuse(f"job {job} is already reserved (in progress, interrupted, or done); use a new job id") from None
    (out / "raw").mkdir(parents=True, exist_ok=True)

    inputs = {}
    for kv in a.input:
        key, path = kv.split("=", 1)
        src = Path(path).expanduser().resolve()
        dst = inp / f"{key}{src.suffix}"
        shutil.copyfile(src, dst)
        put(wf, params, key, f"sonoranlab/{job}/{dst.name}")
        inputs[key] = {"source": str(src), "sha256": sha(dst)}
    sets = {}
    for kv in a.set:
        key, raw = kv.split("=", 1)
        sets[key] = value(raw)
        put(wf, params, key, sets[key])
    for key in params:
        if key.startswith("output_prefix"):
            put(wf, params, key, f"bench/{job}/{key.removeprefix('output_prefix').strip('_') or 'out'}")

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
                "need_gib": a.need_gib, "allow_pause": a.allow_pause, "exit_code": rc, "run": run, "raw": raw,
                "reservation": reservation}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({k: manifest[k] for k in ("job", "exit_code", "raw")} | {"wall_s": run.get("wall_s")}, indent=1))
    return rc


if __name__ == "__main__":
    sys.exit(main())
