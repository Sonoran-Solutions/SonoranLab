"""DYL-125: BiRefNet provenance refusal at job admission and its propagation into the CLI/MCP job summaries."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import test_generation as fixtures
from test_animate import Fake
from test_generation import execute, submit

from sonoranlab.api import call, paths
from sonoranlab.comfy import provenance
from sonoranlab.jobs import store

setup = fixtures.setup
WEIGHTS = "RMBG/BiRefNet/BiRefNet-general.safetensors"
REPO = Path(__file__).resolve().parents[3]


def inventory(ctx):
    return {
        "birefnet": provenance.verify_birefnet(
            Path(os.environ["SONORANLAB_WORK"]), models_tsv=ctx.repo / "env/models.tsv"
        )
    }


@pytest.mark.parametrize("kind", ["animate", "pose_frame"])
@pytest.mark.parametrize("problem", ["missing", "mismatch"])
def test_admission_refuses_unverified_birefnet(setup, kind, problem):
    ctx, animate, pose, _ = setup
    weights = Path(os.environ["SONORANLAB_WORK"]) / WEIGHTS
    weights.unlink() if problem == "missing" else weights.write_bytes(b"swapped weights")
    response = call(kind, animate if kind == "animate" else pose, ctx=ctx)
    assert response["ok"] is False and response["error"]["code"] == "refused"
    assert "BiRefNet-general" in response["error"]["message"] and WEIGHTS in response["error"]["message"]
    assert ("missing" if problem == "missing" else "!= recorded") in response["error"]["message"]
    assert response["error"]["hint"] == provenance.HINT
    assert not (paths.state_root() / "jobs").exists()


def test_queued_job_refused_at_run_when_weights_change(setup, monkeypatch):
    job = submit(setup)
    (Path(os.environ["SONORANLAB_WORK"]) / WEIGHTS).write_bytes(b"swapped after admission")
    job = execute(job, Fake(), monkeypatch)
    assert job["state"] == "failed" and job["error"]["code"] == "model_provenance"
    assert job["error"]["stage"] == "prepare" and job["error"]["retryable"] is False
    assert "!= recorded" in job["error"]["message"] and job["error"]["hint"] == provenance.HINT
    assert not (store.output(job) / "animate").exists()  # nothing generated


def _cli(ctx, tool, params):
    bootstrap = (
        "from pathlib import Path\nimport importlib\n"
        "boundary = importlib.import_module('sonoranlab.api.registry')\noriginal = boundary.Context\n"
        f"boundary.Context = lambda: original(Path({str(ctx.repo)!r}))\n"
        "from sonoranlab.api import paths\npaths.Context = boundary.Context\n"
        "import runpy; runpy.run_module('sonoranlab.cli', run_name='__main__')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", bootstrap, "api", tool, "--params", "-"],
        input=json.dumps(params),
        text=True,
        capture_output=True,
        env=dict(os.environ),
        cwd=REPO,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def test_animate_attempt_inventory_in_job_summaries(setup, monkeypatch):
    ctx, *_ = setup
    fake = Fake()
    prov = inventory(ctx)
    assert prov["birefnet"]["verified"]

    def runner(argv, **kwargs):
        proc = fake(argv, **kwargs)
        request = store.load(Path(argv[argv.index("--request") + 1]))
        path = Path(request["out_dir"]) / "result.json"
        raw = store.load(path)
        raw["model_provenance"] = prov
        store.atomic(path, raw)
        return proc

    job = execute(submit(setup), runner, monkeypatch)
    assert job["state"] == "succeeded", job["error"]
    assert all(a["model_provenance"] == prov for a in job["result"]["attempts"])
    status = call("job_status", {"job_id": job["job_id"]}, ctx=ctx)
    result = call("job_result", {"job_id": job["job_id"]}, ctx=ctx)
    for summary in (status["result"]["result_summary"], result["result"]["result"]):
        assert len(summary["attempts"]) == 5
        assert all(a["model_provenance"] == prov for a in summary["attempts"])
    # CLI `sonoranlab api` (the MCP server's registry boundary) reports the same summary
    assert _cli(ctx, "job_result", {"job_id": job["job_id"]})["result"]["result"] == result["result"]["result"]


def test_pose_raw_result_inventory_and_legacy_attempts(setup, monkeypatch):
    ctx, *_ = setup
    prov = inventory(ctx)

    def runner(argv, **kwargs):
        request = store.load(Path(argv[argv.index("--request") + 1]))
        out = Path(request["out_dir"])
        out.mkdir()
        store.atomic(out / "result.json", {"request": request, "status": "ok", "error": None, "model_provenance": prov})
        (out / "pose.png").write_bytes(b"fake png")
        return subprocess.CompletedProcess(argv, 0)

    job = execute(submit(setup, "pose_frame"), runner, monkeypatch)
    assert job["state"] == "succeeded", job["error"]
    summary = call("job_status", {"job_id": job["job_id"]}, ctx=ctx)["result"]["result_summary"]
    assert summary["raw_result"]["model_provenance"] == prov
    cli = _cli(ctx, "job_status", {"job_id": job["job_id"]})["result"]["result_summary"]
    assert cli["raw_result"]["model_provenance"] == prov

    # A pre-DYL-125 animate result (no inventory) summarises as None, never as a claimed hash.
    from sonoranlab.api.tools.jobs import _result_summary

    legacy = {"job_id": job["job_id"], "output_dir": job["output_dir"]}
    legacy["result"] = {
        "animate": {"outcome": "pass"},
        "attempts": [{"result": "x", "models": {"BiRefNet-general": "unknown"}}],
    }
    assert _result_summary(legacy, {})["attempts"] == [{"result": "x", "model_provenance": None}]
