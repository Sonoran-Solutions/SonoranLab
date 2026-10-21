"""First discovery stays off the loop and imports do not reconcile jobs."""

import importlib
import threading
import time

import anyio
from mcp import types
from test_stdio import run_async

from sonoranlab.jobs import store
from sonoranlab.mcp import server


def test_discovery_with_held_job_lock_keeps_capabilities_responsive(tmp_path, monkeypatch):
    monkeypatch.setenv("SONORANLAB_STATE", str(tmp_path / "state"))
    job = store.directory("jobs/job-20261020T000000Z-00000001", create=True)
    held, release, discovery = threading.Event(), threading.Event(), threading.Event()

    def hold():
        with store.lock(job / "job.lock"):
            held.set()
            release.wait(3)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(1)
    original = server.registry

    def registry():
        discovery.set()
        with store.lock(job / "job.lock"):
            return original()

    monkeypatch.setattr(server, "registry", registry)

    async def exercise():
        listed = []

        async def list_tools():
            listed.append(await server.list_tools(None, None))

        try:
            async with anyio.create_task_group() as tasks:
                started = time.monotonic()
                tasks.start_soon(list_tools)
                while not discovery.is_set():
                    await anyio.sleep(0.01)
                # Reload mimics initial discovery even when another test imported this module.
                await anyio.to_thread.run_sync(importlib.reload, importlib.import_module("sonoranlab.api.tools.jobs"))
                caps = await server.call_tool(None, types.CallToolRequestParams(name="capabilities", arguments={}))
                assert caps.structured_content["ok"]
                assert time.monotonic() - started < 1
                assert not listed
                release.set()
            assert listed[0].tools
        finally:
            release.set()

    try:
        run_async(exercise)
    finally:
        release.set()
        holder.join(timeout=4)
    assert not holder.is_alive()
