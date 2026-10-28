"""Admission is offline and side-effect free; real pipelines use fake generation runners."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from comfy.birefnet_fixture import write_birefnet
from test_animate import Fake
from test_pipeline import GEN, ORDER, stills_dir, toy_profile

from sonoranlab import registry
from sonoranlab.api import Context, ToolError, call, paths
from sonoranlab.artifacts import sha256_file
from sonoranlab.jobs import manager, store, worker
from sonoranlab.jobs.executors import JobFailure
from sonoranlab.jobs.kinds import generation

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    profile = toy_profile(tmp_path)
    repo = tmp_path / "repo"
    (repo / "profiles").mkdir(parents=True)
    data = yaml.safe_load(profile.read_text())
    data["palette"]["source"] = str(tmp_path / "palette.png")
    data["export"]["godot_project"] = str(tmp_path / "game")
    data["animations"]["idle"]["track_default"] = "E"
    data["animations"]["attack"] = {"frames": 2, "fps": 5, "loop": False, "track_default": "C"}
    (repo / "profiles/toy.yaml").write_text(yaml.safe_dump(data))
    shutil.copytree(REPO / "workflows", repo / "workflows")
    shutil.copytree(REPO / "templates", repo / "templates")
    # Synthetic BiRefNet files + models.tsv (DYL-125 admission check); never the host's ~/sonoranlab-work.
    (repo / "env").mkdir()
    monkeypatch.setenv("SONORANLAB_WORK", str(tmp_path / "work"))
    write_birefnet(tmp_path / "work", repo / "env/models.tsv")
    ctx = Context(repo)
    monkeypatch.setattr(paths, "Context", lambda: ctx)
    monkeypatch.setattr(generation, "Context", lambda: ctx)
    monkeypatch.setattr(manager, "launch", lambda j: None)
    monkeypatch.setattr(manager, "_launcher", lambda: "popen")
    monkeypatch.setenv("SONORANLAB_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", str(tmp_path))
    monkeypatch.delenv("SONORANLAB_JOB_EXECUTORS", raising=False)
    monkeypatch.delenv("SONORANLAB_ALLOW_PAUSE_UNITS", raising=False)
    refs = stills_dir(tmp_path)
    registered = registry.register_character("toy", "hero", str(refs), ctx=ctx)
    ref_id = registered["ref_set_id"]
    # Fixture owner evidence, never an agent invocation of the owner CLI.
    base = paths.state_root() / "registry/toy/hero"
    approval = base / "approvals/owner.json"
    approval.parent.mkdir()
    approval.write_text(
        json.dumps(
            {
                "schema": "sonoranlab.ref-approval/1",
                "project": "toy",
                "character": "hero",
                "ref_set_id": ref_id,
                "hashes": {d: sha256_file(refs / f"still_{d}.png") for d in ORDER},
                "reviewer": "fixture owner",
                "evidence": "fixture inspection",
                "approved_at": "2026-10-20T00:00:00Z",
            }
        )
    )
    animate = {
        "project": "toy",
        "character": "hero",
        "ref_set_id": ref_id,
        "anim": "idle",
        "seeds": [1],
        "godot_check": False,
    }
    pose = {
        "project": "toy",
        "character": "hero",
        "ref_set_id": ref_id,
        "direction": "east",
        "template": "idle/east/pose_000",
        "seed": 1,
    }
    target = repo / "templates/poses/idle/east"
    target.mkdir(parents=True)
    shutil.copyfile(repo / "templates/poses/run/east/pose_000.png", target / "pose_000.png")
    return ctx, animate, pose, approval


def submit(setup, kind="animate", **changes):
    ctx, animate, pose, _ = setup
    result = call(kind, (animate if kind == "animate" else pose) | changes, ctx=ctx)
    assert result["ok"], result
    return manager.get(result["result"]["job_id"])


def execute(job, runner, monkeypatch):
    original = worker.Context.run

    def fake(self, argv, **kwargs):
        self.check_cancelled()
        result = runner(argv, cwd=kwargs["cwd"], check=False)
        self.check_cancelled()
        if result.returncode in (3, 4):
            raise JobFailure(
                "budget_refused" if result.returncode == 3 else "guard_busy", "generate", "fake guard", True
            )
        return result

    monkeypatch.setattr(worker.Context, "run", fake)
    worker._work(store.job_dir(job["job_id"]))
    monkeypatch.setattr(worker.Context, "run", original)
    return manager.get(job["job_id"])


@pytest.mark.parametrize(
    "kind,changes",
    [
        ("animate", {"seeds": [1, 2, 3, 4, 5]}),
        ("animate", {"seeds": [1, 1]}),
        ("animate", {"seeds": [-1]}),
        ("animate", {"seeds": [2**31]}),
        ("animate", {"seeds": [1.0]}),
        ("animate", {"need_gib": 39}),
        ("animate", {"anim": "missing"}),
        ("animate", {"anim": "attack"}),
        ("animate", {"allow_pause": ["foreign.service"]}),
        ("animate", {"workflow": "bad"}),
        ("animate", {"track": "C"}),
        ("animate", {"prompt": "bad"}),
        ("pose_frame", {"template": "/tmp/pose_000.png"}),
        ("pose_frame", {"template": "../east/pose_000"}),
        ("pose_frame", {"template": "idle/east/../pose_000"}),
        ("pose_frame", {"direction": "west"}),
        ("pose_frame", {"template": "idle/north/pose_000"}),
        ("pose_frame", {"allow_pause": ["foreign.service"]}),
        ("pose_frame", {"need_gib": 60}),
        ("pose_frame", {"extra": True}),
    ],
)
def test_invalid_writes_nothing(setup, kind, changes, monkeypatch):
    ctx, animate, pose, _ = setup
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("remote call"))
    response = call(kind, (animate if kind == "animate" else pose) | changes, ctx=ctx)
    assert not response["ok"], response
    assert not (paths.state_root() / "jobs").exists()
    assert not (paths.state_root() / "artifacts").exists()


@pytest.mark.parametrize("kind", ["animate", "pose_frame"])
@pytest.mark.parametrize("problem", ["unapproved", "tampered"])
def test_reference_admission(setup, kind, problem):
    ctx, animate, pose, approval = setup
    if problem == "unapproved":
        approval.unlink()
    else:
        still = approval.parent.parent / "refs" / animate["ref_set_id"] / "still_east.png"
        still.chmod(0o644)
        still.write_bytes(b"tampered")
    response = call(kind, animate if kind == "animate" else pose, ctx=ctx)
    assert response["error"]["code"] == "refused"
    assert response["error"]["hint"] == "owner must run `sonoranlab registry approve-references`"
    assert not (paths.state_root() / "jobs").exists()
    assert not (paths.state_root() / "artifacts").exists()


@pytest.mark.parametrize("lift,verdict", [({}, "pass"), ({"north": 3}, "fail")])
def test_pipeline_result(setup, monkeypatch, lift, verdict):
    fake = Fake(lift=lift)

    def runner(argv, **kwargs):
        proc = fake(argv, **kwargs)
        request = store.load(Path(argv[argv.index("--request") + 1]))
        path = Path(request["out_dir"]) / "result.json"
        raw = store.load(path)
        raw.update(
            models={"fixture-model": "a" * 64},
            workflow_source_sha256="b" * 64,
            workflow_patched_sha256="c" * 64,
            inputs={"source": "d" * 64},
            outputs={"frame": "e" * 64},
        )
        store.atomic(path, raw)
        return proc

    job = execute(submit(setup), runner, monkeypatch)
    assert job["state"] == "succeeded", job["error"]
    result = job["result"]
    assert job["stage"] == "validate_export"
    assert job["timing"]["per-stage"] == {k: v for k, v in result["animate"]["timing"].items() if k in store.STAGES}
    assert result["technical"]["validator"] == verdict
    assert result["human"]["production_usable"] is False
    assert result["human"]["message"] == "awaiting owner review"
    assert result["animate"]["inputs"]["directions"] == GEN
    assert len(result["animate"]["inputs"]["reference"]) == 8
    assert result["animate"]["stills"]["directions"] == ORDER
    assert len(result["attempts"]) == 5 and all(r["memory"] is None for r in result["attempts"])
    for attempt in result["attempts"]:
        assert attempt["models"] == {"fixture-model": "a" * 64}
        assert attempt["workflow_source_sha256"] == "b" * 64
        assert attempt["workflow_patched_sha256"] == "c" * 64
        assert attempt["inputs"] == {"source": "d" * 64} and attempt["outputs"] == {"frame": "e" * 64}
    assert bool(result["export_artifacts"]) == (verdict == "pass")
    assert all(float(a[a.index("--need-gib") + 1]) >= 40 for a in fake.argv)
    assert all(a[-2:] == ["--exec", "sonoranlab-comfy"] for a in fake.argv)
    progress = generation.EXECUTORS["animate"].stage(job)
    assert progress["completed"] == progress["planned"] == 5
    assert progress["technical"]["validator"] == verdict
    if verdict == "pass":
        # A simulated owner review of fixture art must be read afresh, then invalidated on tampering.
        from test_review import PASS

        from sonoranlab.review import record_review

        final = Path(result["animate"]["verdict"]["job"])
        record_review(final, **PASS)
        assert generation.EXECUTORS["animate"].stage(job)["human"]["production_usable"] is True
        exported = next((final / "export").rglob("*.png"))
        exported.write_bytes(b"changed fixture output")
        human = generation.EXECUTORS["animate"].stage(job)["human"]
        assert human["review"] == "invalid" and human["production_usable"] is False


@pytest.mark.parametrize("code,error", [(3, "budget_refused"), (4, "guard_busy")])
def test_guard_outcomes(setup, monkeypatch, code, error):
    job = execute(submit(setup), Fake(refuse=(1, code)), monkeypatch)
    assert job["state"] == "failed" and job["error"]["code"] == error
    assert job["error"]["retryable"] is True
    doc = store.load(store.output(job) / "animate/animate.json")
    assert doc["outcome"] == "blocked" and doc["counts"]["not_run"] == 4
    assert job["stage"] == "generate"
    assert job["timing"]["per-stage"] == {k: v for k, v in doc["timing"].items() if k in store.STAGES and v is not None}


def test_retry_limit_and_idempotency(setup, monkeypatch):
    first = execute(submit(setup, idempotency_key="first"), Fake(fail=[("east", 1)]), monkeypatch)
    assert first["state"] == "failed"
    second = manager.retry_job(first["job_id"])
    second = execute(manager.get(second["job_id"]), Fake(fail=[("east", 1)]), monkeypatch)
    ctx, animate, _, _ = setup
    for job in (first, second):  # a real failed generation leaves its durable submission record
        (store.output(job) / "submission.json").write_text(json.dumps({"prompt_id": f"p-{job['job_id']}"}))
        manager.get(job["job_id"])
    with pytest.raises(ToolError, match="unresolved prompts"):
        manager.retry_job(second["job_id"])
    # Reconcile the older attempt too; replay its retained key without reserving new work.
    assert manager.retry_job(first["job_id"], "first")["job_id"] == first["job_id"]
    third = manager.retry_job(second["job_id"])
    third = manager.get(third["job_id"])
    assert third["parent_job_id"] == second["job_id"] and third["attempt"] == 3
    assert len({j["output_dir"] for j in (first, second, third)}) == 3
    assert submit(setup, idempotency_key="first")["job_id"] == first["job_id"]
    response = call("animate", animate | {"seeds": [2]}, ctx=ctx)
    assert response["error"]["code"] == "refused" and "attempt limit" in response["error"]["message"]
    with pytest.raises(ToolError, match=r"attempt limit|already active"):
        manager.retry_job(second["job_id"])


@pytest.mark.parametrize("status", ["ok", "failed"])
def test_pose_guard_and_raw_result(setup, monkeypatch, status):
    def runner(argv, **kwargs):
        assert argv[argv.index("--need-gib") + 1] == "60"
        assert "--gtt-credit" not in argv
        request = store.load(Path(argv[argv.index("--request") + 1]))
        assert request["track"] == "C"
        from sonoranlab.comfy.adapters.track_c import POSE_PROMPTS

        assert request["prompt"] == POSE_PROMPTS["east"]
        out = Path(request["out_dir"])
        out.mkdir()
        error = {"code": "adapter_test", "stage": "collect", "message": "failed pose", "retryable": False}
        store.atomic(
            out / "result.json", {"request": request, "status": status, "error": error if status == "failed" else None}
        )
        (out / "pose.png").write_bytes(b"fake png")
        return subprocess.CompletedProcess(argv, int(status == "failed"))

    job = execute(submit(setup, "pose_frame"), runner, monkeypatch)
    assert job["state"] == ("succeeded" if status == "ok" else "failed")
    assert job["need_gib"] == 60 and job["track"] == "C"
    if status == "ok":
        assert job["result"]["raw_result"]["status"] == "ok"
        assert any(ref.endswith(":attempt-1/pose.png") for ref in job["result"]["artifacts"])
    else:
        assert job["error"]["code"] == "adapter_test" and job["error"]["stage"] == "collect"


@pytest.mark.parametrize("kind", ["animate", "pose_frame"])
def test_missing_container_is_unavailable(setup, monkeypatch, kind):
    def runner(argv, **kwargs):
        request = store.load(Path(argv[argv.index("--request") + 1]))
        out = Path(request["out_dir"])
        out.mkdir(parents=True)
        store.atomic(
            out / "result.json",
            {
                "status": "failed",
                "error": {
                    "code": "adapter_exception",
                    "stage": "run",
                    "message": "OSError: podman exec relay failed (rc 125): no such container",
                    "retryable": False,
                },
            },
        )
        return subprocess.CompletedProcess(argv, 1)

    job = execute(submit(setup, kind), runner, monkeypatch)
    assert job["state"] == "failed" and job["error"]["code"] == "unavailable"
    assert job["error"]["hint"] == "start it: bash env/comfy.sh up --offline"


@pytest.mark.parametrize("kind", ["animate", "pose_frame"])
def test_cancel_during_generate_scopes_prompts(setup, monkeypatch, kind):
    calls = []
    foreign = {"foreign-prompt": "running"}

    class Probe:
        def status(self, prompt):
            calls.append(("status", prompt))
            return "stopped"

        def cancel(self, prompt):
            calls.append(("cancel", prompt))
            return True

    monkeypatch.setattr(worker, "prober", Probe)
    monkeypatch.setattr(manager, "_signal", lambda job: True)
    job = submit(setup, kind)

    def runner(argv, **kwargs):
        request = store.load(Path(argv[argv.index("--request") + 1]))
        out = Path(request["out_dir"])
        out.mkdir(parents=True)
        store.atomic(out / "submission.json", {"prompt_id": "owned-prompt"})
        assert manager.cancel_job(job["job_id"], probe=Probe())["state"] == "cancelling"
        return subprocess.CompletedProcess(argv, -15)

    finished = execute(job, runner, monkeypatch)
    assert finished["state"] == "cancelled", finished
    # The worker cancels its own unresolved prompt and confirms it stopped; nothing else is touched.
    assert calls == [("cancel", "owned-prompt"), ("status", "owned-prompt")]
    assert foreign == {"foreign-prompt": "running"}
    assert len(finished["prompts"]) == 1


def test_input_provenance_and_owner_pause(setup, monkeypatch):
    monkeypatch.setenv("SONORANLAB_ALLOW_PAUSE_UNITS", "owned.service")
    fake = Fake(refuse=(1, 3))
    job = submit(setup, allow_pause=["owned.service"])
    assert set(job["input_hashes"]) == {"stills", "profile", "workflow"}
    assert len(job["input_hashes"]["stills"]) == 8
    execute(job, fake, monkeypatch)
    argv = fake.argv[0]
    assert argv[argv.index("--allow-pause") + 1] == "owned.service"
    assert argv.index("--allow-pause") < argv.index("--")


def test_queued_inputs_rechecked(setup, monkeypatch):
    job = submit(setup)
    setup[3].unlink()
    fake = Fake()
    finished = execute(job, fake, monkeypatch)
    assert finished["state"] == "failed" and not fake.calls


def test_template_symlink_and_missing_prompt_refused(setup, monkeypatch):
    ctx, _, pose, _ = setup
    target = ctx.repo / "templates/poses/idle/east/pose_000.png"
    target.unlink()
    target.symlink_to(ctx.repo.parent / "palette.png")
    response = call("pose_frame", pose, ctx=ctx)
    assert response["error"]["code"] == "path_refused"
    target.unlink()
    shutil.copyfile(ctx.repo / "templates/poses/run/east/pose_000.png", target)
    from sonoranlab.comfy.adapters import track_c

    monkeypatch.setattr(track_c, "POSE_PROMPTS", {})
    response = call("pose_frame", pose, ctx=ctx)
    assert response["error"]["code"] == "refused"
    assert not (paths.state_root() / "jobs").exists()


def test_mcp_cli_submit_status_parity(setup, tmp_path):
    import os
    import sys

    import anyio
    from mcp import ClientSession, StdioServerParameters, stdio_client
    from test_stdio import BOOTSTRAP, run_async

    ctx, animate, pose, _ = setup
    patch = (
        "from pathlib import Path\nimport importlib\n"
        "boundary=importlib.import_module('sonoranlab.api.registry')\n"
        f"ctx=boundary.Context(Path({str(ctx.repo)!r}))\n"
        "boundary.Context=lambda:ctx\n"
        "from sonoranlab.api import paths\npaths.Context=lambda:ctx\n"
        "from sonoranlab.jobs import manager\n"
        "manager.launch=lambda job:None\nmanager._launcher=lambda:'popen'\n"
        "from sonoranlab.jobs.kinds import generation\ngeneration.Context=lambda:ctx\n"
    )
    env = dict(os.environ)

    def cli(name, params):
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                patch + "\nimport runpy; runpy.run_module('sonoranlab.cli',run_name='__main__')",
                "api",
                name,
                "--params",
                "-",
            ],
            input=json.dumps(params),
            text=True,
            capture_output=True,
            timeout=20,
            cwd=REPO,
            env=env,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return json.loads(proc.stdout)

    async def exercise():
        parameters = StdioServerParameters(command=sys.executable, args=["-c", patch + BOOTSTRAP], env=env, cwd=REPO)
        with anyio.fail_after(60), (tmp_path / "mcp.log").open("w+") as log:
            async with stdio_client(parameters, errlog=log) as streams, ClientSession(*streams) as session:
                await session.initialize()
                advertised = {t.name: t for t in (await session.list_tools()).tools}
                for kind, params in (("animate", animate), ("pose_frame", pose)):
                    assert advertised[kind].input_schema == (
                        generation.ANIMATE_SCHEMA if kind == "animate" else generation.POSE_SCHEMA
                    )
                    params = params | {"idempotency_key": kind + "-parity"}
                    response = await session.call_tool(kind, params)
                    assert not response.is_error, response
                    assert response.structured_content == cli(kind, params)
                    assert json.loads(response.content[0].text) == response.structured_content
                    arguments = {"job_id": response.structured_content["result"]["job_id"]}
                    status = await session.call_tool("job_status", arguments)
                    assert status.structured_content == cli("job_status", arguments)

    run_async(exercise)
