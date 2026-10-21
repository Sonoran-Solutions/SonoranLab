"""Real SDK subprocess transport and CLI parity; no GPU/editor/engine processes."""

import asyncio
import importlib
import json
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp import types as mcp_types

from sonoranlab.api import Tool, registry
from sonoranlab.api.registry import ENVELOPE_SCHEMA

REPO = Path(__file__).resolve().parents[3]

# ponytail: this execution sandbox blocks AF_UNIX wakeup writes. A test-only timer
# keeps asyncio's selector waking; remove it when the sandbox permits self-pipe writes.
TIMER = textwrap.dedent("""
    import asyncio
    original_loop_factory = asyncio.events.new_event_loop
    def timer_loop_factory():
        loop = original_loop_factory()
        def tick():
            loop.call_later(0.01, tick)
        tick()
        return loop
    asyncio.events.new_event_loop = timer_loop_factory
""")

AUDIT = textwrap.dedent("""
    import sys
    def no_processes(event, args):
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.fork"}:
            raise AssertionError("server attempted a subprocess: " + event)
    sys.addaudithook(no_processes)
    def check_modules():
        assert not any(name.split('.')[0] in {"torch", "pixeloe"} for name in sys.modules)
    import sonoranlab.mcp.server as server
    check_modules()
""")

BOOTSTRAP = (
    TIMER
    + AUDIT
    + textwrap.dedent("""
    import logging
    import runpy
    original_create_server = server.create_server
    def audited_server():
        instance = original_create_server()
        async def audit_initialize(ctx, call_next):
            result = await call_next(ctx)
            check_modules()
            if ctx.method == "initialize":
                logging.getLogger("startup-test").warning("initialize audit: no heavy imports or subprocesses")
            return result
        instance.middleware.append(audit_initialize)
        return instance
    server.create_server = audited_server
    sys.argv = ["sonoranlab", "mcp", "serve"]
    runpy.run_module("sonoranlab.cli", run_name="__main__")
""")
)


def timer_loop_factory():
    loop = asyncio.new_event_loop()

    def tick():
        loop.call_later(0.01, tick)

    tick()
    return loop


def run_async(fn):
    return anyio.run(fn, backend_options={"loop_factory": timer_loop_factory})


def test_import_is_light_and_does_not_spawn():
    result = subprocess.run(
        [sys.executable, "-c", AUDIT + '\nprint("clean import")'],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout == "clean import\n"
    assert result.stderr == ""


def test_stdio_initialize_discovery_calls_parity_and_logging(tmp_path, caplog, monkeypatch):
    # The in-process registry() used for comparison must see the same (absent) editor runtime as the server.
    monkeypatch.setenv("SONORANLAB_EDITOR_RUNTIME", str(tmp_path / "missing-runtime"))
    env = dict(
        os.environ,
        SONORANLAB_STATE=str(tmp_path / "state"),
        SONORANLAB_EDITOR_RUNTIME=str(tmp_path / "missing-runtime"),
    )
    stderr_path = tmp_path / "server-stderr.log"

    async def exercise():
        with anyio.fail_after(20), stderr_path.open("w+") as errlog:
            params = StdioServerParameters(command=sys.executable, args=["-c", BOOTSTRAP], env=env, cwd=REPO)
            async with stdio_client(params, errlog=errlog) as streams, ClientSession(*streams) as session:
                initialized = await session.initialize()
                assert initialized.server_info.name == "sonoranlab"
                advertised = (await session.list_tools()).tools
                assert {tool.name for tool in advertised} == set(registry())
                for tool in advertised:
                    registered = registry()[tool.name]
                    assert tool.input_schema == registered.input_schema
                    assert tool.output_schema == ENVELOPE_SCHEMA
                    for key, value in registered.annotations.items():
                        assert tool.annotations.model_dump(by_alias=True)[key] == value
                    if tool.name not in ("capabilities", "list_projects", "get_profile"):
                        continue  # later tools have their own stdio/CLI parity tests with fixtures
                    args = {"project": "twos-are-up"} if tool.name == "get_profile" else {}
                    result = await session.call_tool(tool.name, args)
                    assert not result.is_error
                    envelope = result.structured_content
                    Draft202012Validator(ENVELOPE_SCHEMA).validate(envelope)
                    assert json.loads(result.content[0].text) == envelope
                    cli = subprocess.run(
                        [sys.executable, "-m", "sonoranlab.cli", "api", tool.name, "--params", "-"],
                        input=json.dumps(args),
                        env=env,
                        cwd=REPO,
                        text=True,
                        capture_output=True,
                        timeout=10,
                        check=True,
                    )
                    assert json.loads(cli.stdout) == envelope
                for name, args in (("get_profile", {}), ("capabilities", {"extra": 1}), ("missing", {})):
                    result = await session.call_tool(name, args)
                    assert result.is_error
                    Draft202012Validator(ENVELOPE_SCHEMA).validate(result.structured_content)
                    assert result.structured_content["error"]["code"] == "invalid_params"
                assert await session.send_ping() is not None

    run_async(exercise)
    log = stderr_path.read_text()
    assert "SonoranLab stdio server ready" in log
    assert "initialize audit: no heavy imports or subprocesses" in log
    assert "Traceback" not in log
    assert not any(
        "parse" in record.message.lower() or "invalid json" in record.message.lower() for record in caplog.records
    )
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("read_only", [True, False])
def test_handlers_run_off_event_loop_and_remain_responsive(monkeypatch, read_only):
    from sonoranlab.mcp.server import call_tool

    parent_thread = threading.get_ident()
    started, release = threading.Event(), threading.Event()

    def slow(params, ctx):
        assert threading.get_ident() != parent_thread
        started.set()
        assert release.wait(5), "event loop never released the slow handler"
        return {"complete": True}

    tool = Tool("slow", "test", {"type": "object"}, read_only, "sync", slow)
    modules = importlib.import_module("sonoranlab.api.tools")
    monkeypatch.setattr(modules, "MODULES", (SimpleNamespace(TOOLS=(tool,)),))
    results = []

    async def exercise():
        async def invoke():
            results.append(await call_tool(None, mcp_types.CallToolRequestParams(name="slow", arguments={})))

        with anyio.fail_after(8):
            async with anyio.create_task_group() as tg:
                tg.start_soon(invoke)
                while not started.is_set():
                    await anyio.sleep(0.01)
                # This task can only release the handler if it isn't blocking the loop.
                release.set()

    run_async(exercise)
    assert results[0].structured_content["result"] == {"complete": True}
