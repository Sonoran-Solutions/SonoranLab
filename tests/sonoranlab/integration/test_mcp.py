"""Adversarial Phase 4 fault matrix through official SDK stdio subprocesses only."""

import json
import os
import signal
import subprocess
import sys
import time

import anyio
import pytest
from conftest import REPO, call, run_async, status, wait_file
from jsonschema import Draft202012Validator

from sonoranlab.api.registry import ENVELOPE_SCHEMA

CORE = {
    "capabilities",
    "list_projects",
    "get_profile",
    "list_characters",
    "get_character",
    "list_jobs",
    "job_status",
    "job_result",
    "get_artifact",
    "check_review",
    "check_class_request",
    "import_job",
    "register_character",
    "validate",
    "contact_sheet",
    "compare",
    "animate",
    "pose_frame",
    "process",
    "export_godot",
    "godot_check",
    "smoke_render",
    "cancel_job",
    "retry_job",
    # DYL-129 campaign ledger
    "campaign_create",
    "campaign_status",
    "campaign_submit",
    "campaign_decide",
    "campaign_escalate",
    "campaign_report",
    "campaign_note",
    # DYL-130 /new-class
    "view_artifact",
    "compose_sheet",
    "build_delivery",
    "delivery_status",
}
REPAIR = {"prepare_repair", "inspect_repair", "inspect_repair_diff", "finalize_repair", "abort_repair"}
READ = {
    "capabilities",
    "list_projects",
    "get_profile",
    "list_characters",
    "get_character",
    "list_jobs",
    "job_status",
    "job_result",
    "get_artifact",
    "check_review",
    "check_class_request",
}
CAMPAIGN_READ = {"campaign_status", "campaign_report"}  # read-only too; exercised in tests/sonoranlab/campaign
NEWCLASS_READ = {"view_artifact", "delivery_status"}  # read-only too; exercised in tests/sonoranlab/newclass
BASE = {"project": "toy", "character": "hero", "anim": "idle"}


def assert_editor_mode_discovery(unavailable, available):
    """Core tools are identical in both modes; the five optional repair tools appear only with the editor runtime."""
    assert set(unavailable) == CORE
    assert {k: v for k, v in available.items() if k not in REPAIR} == unavailable
    assert not (set(unavailable) & REPAIR)
    assert set(available) - set(unavailable) == REPAIR


def events(lab, event):
    path = lab.state / "fake/events.jsonl"
    return [row for line in path.read_text().splitlines() if (row := json.loads(line))["event"] == event]


async def submit_process(lab, session, **changes):
    result = await call(session, "process", BASE | {"raw": str(lab.raw), "mode": "raw"} | changes)
    return result["job_id"]


def test_project_registration_and_editor_discovery(lab):
    config = json.loads((REPO / ".mcp.json").read_text())
    assert config == {
        "mcpServers": {
            "sonoranlab": {
                "command": "uv",
                "args": ["run", "--project", ".", "--frozen", "--offline", "sonoranlab", "mcp", "serve"],
            }
        }
    }

    async def exercise():
        discovered = []
        runtime = lab.root / "runtime"
        runtime.mkdir()
        for configured, editor in ((True, lab.root / "missing-runtime"), (False, runtime)):
            async with lab.client(configured=configured, SONORANLAB_EDITOR_RUNTIME=str(editor)) as (client, _):
                caps = await call(client, "capabilities")
                assert caps["comfyui"]["status"] == "not_probed"
                assert caps["godot"]["path"] is None
                assert caps["editor"] == ("available" if editor == runtime else "unavailable")
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                metadata = {tool["name"]: tool for tool in caps["tools"]}
                assert set(metadata) == set(tools)
                for name, tool in tools.items():
                    assert tool.input_schema == metadata[name]["input_schema"]
                    Draft202012Validator.check_schema(tool.input_schema)
                    assert tool.input_schema["additionalProperties"] is False
                    assert tool.output_schema == ENVELOPE_SCHEMA
                    assert tool.annotations.model_dump(by_alias=True, exclude_none=True) == {
                        "readOnlyHint": name in READ | CAMPAIGN_READ | NEWCLASS_READ
                        or name.startswith("inspect_repair"),
                        "destructiveHint": False,
                        "idempotentHint": name in READ | CAMPAIGN_READ | NEWCLASS_READ
                        or name.startswith("inspect_repair"),
                    }
                discovered.append({k: v.model_dump() for k, v in tools.items()})
                for name, params in (
                    ("get_profile", {}),
                    ("capabilities", {"extra": 1}),
                    ("register_character", {"project": "../toy"}),
                    ("unknown", {}),
                ):
                    await call(client, name, params, error="invalid_params")
        assert_editor_mode_discovery(*discovered)

    run_async(exercise)


def test_registration_owner_approval_paths_and_stale_stills(lab):
    async def exercise():
        async with lab.client() as (client, _):
            for evil in (str(lab.stills / ".." / "stills"), str(lab.root / "escape"), str(lab.root / "game")):
                if evil.endswith("escape"):
                    (lab.root / "escape").symlink_to(lab.root.parent, target_is_directory=True)
                await call(client, "register_character", lab.base | {"reference_dir": evil}, error="path_refused")
            unapproved = await call(client, "register_character", lab.base | {"reference_dir": str(lab.stills)})
            assert unapproved["approval"]["state"] == "unapproved"
            params = BASE | {"ref_set_id": unapproved["ref_set_id"], "seeds": [1]}
            await call(client, "animate", params, error="refused")
            params = await lab.references(client)
            character = await call(client, "get_character", lab.base)
            assert character["reference_sets"][0]["approval"]["state"] == "approved"
            still = lab.state / "registry/toy/hero/refs" / params["ref_set_id"] / "still_east.png"
            assert still.stat().st_mode & 0o222 == 0
            still.chmod(0o644)
            still.write_bytes(b"tampered synthetic still")
            await call(client, "animate", params, error="refused")
            character = await call(client, "get_character", lab.base)
            assert character["reference_sets"][0]["approval"]["state"] == "invalid"
            assert not (lab.state / "jobs").exists()
            assert list((lab.root / "game").iterdir()) == []

    run_async(exercise)


def test_slow_job_status_result_and_client_disconnect(lab):
    async def exercise():
        async with lab.client() as (client, _):
            job = await submit_process(lab, client)
            out = lab.output(job)
            await wait_file(out / "ready")
            for _ in range(4):
                started = time.monotonic()
                current = await call(client, "job_status", {"job_id": job})
                assert time.monotonic() - started < 1
                assert current["state"] == "running"
            await call(client, "job_result", {"job_id": job}, error="not_ready")
        # Client/server lifetime ends while the detached worker still owns the job.
        async with lab.client() as (client, _):
            assert (await call(client, "job_status", {"job_id": job}))["state"] == "running"
            (out / "release").touch()
            await status(client, job)
            result = await call(client, "job_result", {"job_id": job})
            assert result["result"]["complete"]
            assert any(a["ref"] == f"{job}:artifact.txt" for a in result["artifacts"])
            assert all(set(a) == {"ref"} for a in result["artifacts"])
            assert len(events(lab, "start")) == 1

    run_async(exercise)


@pytest.mark.parametrize("submitted", [False, True], ids=["lost-worker", "lost-reply"])
def test_server_restart_live_lease_then_worker_loss_never_resubmits(lab, submitted):
    async def exercise():
        async with lab.client() as (client, host):
            if submitted:
                params = await lab.references(client)
                job = (await call(client, "animate", params))["job_id"]
            else:
                job = await submit_process(lab, client)
            out = lab.output(job)
            await wait_file(out / ("submitted" if submitted else "ready"))
            worker = (await call(client, "job_status", {"job_id": job}))["worker"]["pid"]
            host.killed = True
            os.kill(host.pid, signal.SIGKILL)
        async with lab.client() as (client, _):
            current = await call(client, "job_status", {"job_id": job})
            assert current["state"] == "running" and current["worker"]["pid"] == worker
            os.kill(worker, signal.SIGKILL)
            expected = "blocked" if submitted else "failed"
            current = await status(client, job, (expected,))
            assert current["error"]["code"] == ("needs_reconciliation" if submitted else "worker_lost")
            if submitted:
                prompt = json.loads((out / "gen/submission.json").read_text())["prompt_id"]
                assert not (out / "gen/result.json").exists()
                assert prompt in current["error"]["message"]
                await call(client, "retry_job", {"job_id": job}, error="conflict")
                assert (await call(client, "cancel_job", {"job_id": job}))["state"] == "cancelled"
                assert [e["prompt_id"] for e in events(lab, "cancel")] == [prompt]
            for _ in range(3):
                await call(client, "list_jobs")
            assert len(events(lab, "start")) == 1
            assert len(events(lab, "submit")) == int(submitted)
            assert len(list((lab.state / "jobs").glob("job-*"))) == 1

    run_async(exercise)


def test_concurrent_clients_idempotency_and_duplicate_active(lab):
    async def exercise():
        async with lab.client() as (one, _), lab.client() as (two, _):
            results = []

            async def submit(client):
                results.append(await submit_process(lab, client, idempotency_key="same"))

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(submit, one)
                tasks.start_soon(submit, two)
            assert results[0] == results[1]
            job = results[0]
            await wait_file(lab.output(job) / "ready")
            params = BASE | {"raw": str(lab.raw), "mode": "raw"}
            await call(
                two, "process", params | {"idempotency_key": "same", "godot_check": True}, error="idempotency_conflict"
            )
            await call(two, "process", params, error="conflict")
            assert len(list((lab.state / "jobs").glob("job-*"))) == 1
            (lab.output(job) / "release").touch()
            await status(one, job)
            assert len(events(lab, "start")) == 1

    run_async(exercise)


def test_two_servers_share_gpu_lock(lab):
    async def exercise():
        async with lab.client() as (one, _), lab.client() as (two, _):
            params = await lab.references(one)
            first = (await call(one, "animate", params))["job_id"]
            await wait_file(lab.output(first) / "submitted")
            second = (await call(two, "animate", params | {"seeds": [2]}))["job_id"]
            queued = await call(two, "job_status", {"job_id": second})
            assert queued["state"] == "queued"
            assert not (lab.output(second) / "submitted").exists()
            (lab.output(first) / "release").touch()
            await status(one, first)
            await wait_file(lab.output(second) / "submitted")
            (lab.output(second) / "release").touch()
            await status(two, second)
            starts = {e["job_id"]: e["at"] for e in events(lab, "start")}
            ends = {e["job_id"]: e["at"] for e in events(lab, "end")}
            assert starts[first] < ends[first] <= starts[second] < ends[second]
            assert len(events(lab, "submit")) == 2

    run_async(exercise)


def test_cancel_only_owned_prompt(lab):
    async def exercise():
        async with lab.client() as (client, _):
            params = await lab.references(client)
            job = (await call(client, "animate", params))["job_id"]
            await wait_file(lab.output(job) / "submitted")
            foreign = lab.state / "fake/foreign-prompt.json"
            foreign.write_text('{"state":"running"}')
            prompt = json.loads((lab.output(job) / "gen/submission.json").read_text())["prompt_id"]
            await call(client, "cancel_job", {"job_id": job})
            await status(client, job, ("cancelled",))
            assert [e["prompt_id"] for e in events(lab, "cancel")] == [prompt]
            assert json.loads(foreign.read_text())["state"] == "running"
            assert (await call(client, "cancel_job", {"job_id": job}))["state"] == "cancelled"

    run_async(exercise)


def test_generation_budget_and_pose_guard(lab):
    async def exercise():
        async with lab.client(INTEGRATION_MODE="pose") as (client, _):
            params = await lab.references(client)
            await call(client, "animate", params | {"need_gib": 39}, error="invalid_params")
            assert not (lab.state / "jobs").exists()
            pose = lab.base | {
                "ref_set_id": params["ref_set_id"],
                "direction": "east",
                "template": "idle/east/pose_000",
                "seed": 1,
            }
            job = (await call(client, "pose_frame", pose))["job_id"]
            current = await status(client, job)
            assert current["need_gib"] == 60 and current["track"] == "C"
            frozen = json.loads((lab.state / "jobs" / job / "request.json").read_text())
            assert frozen["params"]["track"] == "C"
            request = json.loads((lab.output(job) / "request.json").read_text())
            assert request["track"] == "C"
            argv = events(lab, "argv")[0]["argv"]
            assert argv[argv.index("--need-gib") + 1] == "60"
            assert "--gtt-credit" not in argv

    run_async(exercise)


def test_validator_fail_succeeds_without_export(lab):
    async def exercise():
        async with lab.client(INTEGRATION_MODE="real") as (client, _):
            (lab.raw / "north_1.png").unlink()
            job = await submit_process(lab, client)
            current = await status(client, job)
            result = await call(client, "job_result", {"job_id": job})
            assert current["technical"]["validator"] == "fail"
            assert result["state"] == "succeeded"
            assert result["result"]["technical"]["validator"] == "fail"
            assert not result["result"]["export"]
            assert not (lab.output(job) / "export").exists()

    run_async(exercise)


@pytest.mark.parametrize("tamper", ["report", "export"])
def test_offline_reads_review_tampering_and_godot_unavailable(lab, tamper):
    # Check both family guards without allocating sockets (the sandbox also denies socket creation).
    check = subprocess.run(
        [
            sys.executable,
            "-c",
            "import socket\nfrom types import SimpleNamespace\n"
            "for family, address in [(socket.AF_INET,('127.0.0.1',9)),(socket.AF_INET6,('::1',9))]:\n"
            "    try: socket.socket.connect(SimpleNamespace(family=family),address)\n"
            "    except OSError as e: assert 'integration: network disabled' in str(e)\n"
            "    else: raise AssertionError('connect escaped')\n",
        ],
        env=lab.env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert check.returncode == 0, check.stderr

    async def exercise():
        async with lab.client(INTEGRATION_MODE="real") as (client, _):
            await lab.references(client)
            job = await submit_process(lab, client, stills=str(lab.stills))
            await status(client, job)
            out = lab.output(job)
            imported = await call(client, "import_job", lab.base | {"job_dir": str(out)})
            lab.owner_cli(
                "review",
                "record",
                str(out),
                "--verdict",
                "pass",
                "--reviewer",
                "fixture owner",
                "--cleanup-minutes",
                "5",
                "--started",
                "2026-10-19T10:00:00+00:00",
                "--ended",
                "2026-10-19T10:05:00+00:00",
            )
            cases = {
                "capabilities": {},
                "list_projects": {},
                "get_profile": {"project": "toy"},
                "list_characters": {"project": "toy"},
                "get_character": lab.base,
                "list_jobs": {},
                "job_status": {"job_id": job},
                "job_result": {"job_id": job},
                "get_artifact": {"ref": job + ":report.json"},
                "check_review": {"job": imported["artifact_id"]},
                "check_class_request": {"project": "toy", "request": {}},
            }
            assert set(cases) == READ
            for name, params in cases.items():
                result = await call(client, name, params)
                if name == "check_review":
                    assert result["production_usable"]
                if name == "check_class_request":
                    assert result["status"] == "invalid"
            assert (await call(client, "job_status", {"job_id": job}))["human"]["production_usable"]
            checked = (await call(client, "godot_check", BASE | {"source": job + ":export/hero/hero.png"}))["job_id"]
            await status(client, checked)
            result = await call(client, "job_result", {"job_id": checked})
            assert result["result"]["technical"]["godot"] == "unavailable"
            assert result["result"]["godot_check"]["godot_attempts"] == []
            target = out / ("report.json" if tamper == "report" else "export/hero/hero.png")
            if tamper == "report":
                doc = json.loads(target.read_text())
                doc["digests"]["north_0"] = "0" * 64
                target.write_text(json.dumps(doc))
            else:
                target.write_bytes(b"changed fixture export")
            review = await call(client, "check_review", {"job": imported["artifact_id"]})
            assert review["review_state"] == "invalid" and not review["production_usable"]
            await call(client, "export_godot", BASE | {"source": job + ":report.json"}, error="refused")
            current = await call(client, "job_status", {"job_id": job})
            assert current["human"]["review"] == "invalid" and not current["human"]["production_usable"]
            assert list((lab.root / "game").iterdir()) == []

    run_async(exercise)


def test_stdout_protocol_stderr_logging_and_bounded_worker_log(lab, caplog):
    async def exercise():
        async with lab.client(INTEGRATION_MODE="noisy") as (client, host):
            await client.list_tools()
            job = await submit_process(lab, client)
            await wait_file(lab.output(job) / "ready")
            (lab.output(job) / "release").touch()
            await status(client, job)
            await call(client, "job_result", {"job_id": job})
            log = lab.state / "jobs" / job / "worker.log"
            assert 0 < log.stat().st_size <= 1024 * 1024
            assert "SonoranLab stdio server ready" in host.log.read_text()
            assert await client.send_ping() is not None

    run_async(exercise)
    assert not any("parse" in r.message.lower() or "invalid json" in r.message.lower() for r in caplog.records)


def test_get_character_merges_native_and_imported_history(lab):
    """DYL-126: one logical entry per job over real stdio, identical to `sonoranlab api get_character`."""

    async def exercise():
        async with lab.client(INTEGRATION_MODE="real") as (client, _):
            await lab.references(client)
            job = await submit_process(lab, client, stills=str(lab.stills))
            await status(client, job)
            imported = await call(client, "import_job", lab.base | {"job_dir": str(lab.output(job))})
            params = lab.base | {"jobs_limit": 1}
            character = await call(client, "get_character", params)
            assert character["job_count"] == 1 and not character["jobs_truncated"]
            (entry,) = character["jobs"]
            assert entry["id"] == job and entry["source"] == "native+import" and entry["status"] == "ok"
            assert imported["artifact_id"] in entry["aliases"] and entry["state"] == "succeeded"
            assert entry["human"] == {"review": "none", "production_usable": False}
            response = await client.call_tool("get_character", params)
            proc = subprocess.run(
                [str(REPO / ".venv/bin/sonoranlab"), "api", "get_character", "--params", "-"],
                input=json.dumps(params),
                env=lab.env,
                cwd=REPO,
                text=True,
                capture_output=True,
                timeout=30,
            )
            assert proc.returncode == 0, proc.stdout + proc.stderr
            assert response.structured_content == json.loads(proc.stdout)

    run_async(exercise)
