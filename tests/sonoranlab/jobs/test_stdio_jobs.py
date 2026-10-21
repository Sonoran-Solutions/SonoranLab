"""Real DYL-113 stdio harness: a detached job survives both MCP client and server exits."""

import json
import os
import runpy
import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client

from sonoranlab.jobs import manager, store

REPO = Path(__file__).resolve().parents[3]
HARNESS = runpy.run_path(str(REPO / "tests/sonoranlab/mcp/test_stdio.py"))


def test_stdio_restart_sees_same_job_and_progress(jobs_env, monkeypatch, tmp_path):
    with monkeypatch.context() as patch:
        patch.setattr(manager, "launch", lambda job: None)
        parent = manager.submit("slow", {})["job_id"]
    manager.cancel_job(parent)
    server_program = HARNESS["TIMER"] + (
        "\nimport sys, runpy\nsys.argv=['sonoranlab','mcp','serve']\n"
        "runpy.run_module('sonoranlab.cli',run_name='__main__')\n"
    )
    parameters = StdioServerParameters(
        command=sys.executable, args=["-c", server_program], env=dict(os.environ), cwd=REPO
    )
    job_id = None

    async def first_pair():
        nonlocal job_id
        with anyio.fail_after(25), (tmp_path / "server-one.log").open("w+") as log:
            async with stdio_client(parameters, errlog=log) as streams, ClientSession(*streams) as session:
                await session.initialize()
                response = await session.call_tool("retry_job", {"job_id": parent})
                assert not response.is_error
                job_id = response.structured_content["result"]["job_id"]
                root = store.output(manager.get(job_id))
                while not (root / "ready").exists():
                    await anyio.sleep(0.01)
                result = await session.call_tool("job_status", {"job_id": job_id})
                assert result.structured_content["result"]["progress"]["ready"]
                assert result.structured_content["result"]["state"] == "running"
                # Do not release the worker until this entire pair has exited.

    HARNESS["run_async"](first_pair)
    assert manager.get(job_id)["state"] == "running"

    async def second_pair():
        with anyio.fail_after(25), (tmp_path / "server-two.log").open("w+") as log:
            async with stdio_client(parameters, errlog=log) as streams, ClientSession(*streams) as session:
                await session.initialize()
                result = await session.call_tool("job_status", {"job_id": job_id})
                envelope = result.structured_content
                assert envelope["result"]["job_id"] == job_id
                assert envelope["result"]["state"] == "running"
                assert envelope["result"]["progress"]["ready"]
                root = store.output(manager.get(job_id))
                (root / "release").touch()
                while manager.get(job_id)["state"] != "succeeded":
                    await anyio.sleep(0.01)
                result = await session.call_tool("job_result", {"job_id": job_id})
                assert not result.is_error
                assert result.structured_content["result"]["result"] == {"complete": True}
                assert json.loads(result.content[0].text) == result.structured_content

    HARNESS["run_async"](second_pair)
