"""Real stdio servers with synthetic repository/state and detached, file-gated workers."""

import importlib
import json
import os
import shutil
import signal
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
import yaml
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters, stdio_client

HERE = Path(__file__).parent
REPO = HERE.parents[2]
for directory in (HERE, HERE.parent, HERE.parent / "mcp", HERE.parent / "jobs"):
    sys.path.insert(0, str(directory))

from comfy.birefnet_fixture import write_birefnet  # noqa: E402
from test_pipeline import raw_set, stills_dir, toy_profile  # noqa: E402
from test_stdio import TIMER, run_async  # noqa: E402, F401

from sonoranlab.api.registry import ENVELOPE_SCHEMA  # noqa: E402

# Context has no environment override: patch only its repository default, in every
# test subprocess (including detached workers and the synthetic owner CLI).
SITECUSTOMIZE = (
    TIMER
    + """
import importlib, json, os, socket, sys
from pathlib import Path
boundary = importlib.import_module("sonoranlab.api.registry")
OriginalContext = boundary.Context
boundary.Context = lambda: OriginalContext(Path(os.environ["INTEGRATION_REPO"]))
if os.environ.get("INTEGRATION_OFFLINE"):
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError("integration: network disabled")
        return original_connect(self, address)
    def connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError("integration: network disabled")
        return original_connect_ex(self, address)
    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
if sys.argv[-2:] == ["mcp", "serve"]:
    from sonoranlab.mcp import server
    original_create = server.create_server
    def create():
        instance = original_create()
        async def audit(ctx, call_next):
            result = await call_next(ctx)
            if ctx.method == "initialize":
                heavy = sorted(n for n in sys.modules if n.split(".")[0] in {"torch", "pixeloe"})
                Path(os.environ["INTEGRATION_SERVER"]).write_text(json.dumps({"pid":os.getpid(), "heavy":heavy}))
                assert not heavy, heavy
            return result
        instance.middleware.append(audit)
        return instance
    server.create_server = create
"""
)


async def wait_file(path):
    with anyio.fail_after(45):
        while not path.exists():
            await anyio.sleep(0.02)  # Poll a durable handshake, never guess a worker's startup time.


async def call(session, name, params=None, *, error=None):
    response = await session.call_tool(name, params or {})
    envelope = response.structured_content
    Draft202012Validator(ENVELOPE_SCHEMA).validate(envelope)
    assert envelope["tool"] == name
    assert json.loads(response.content[0].text) == envelope
    assert response.is_error == (not envelope["ok"])
    if error:
        assert envelope["error"]["code"] == error, envelope
        return envelope["error"]
    assert envelope["ok"], envelope
    return envelope["result"]


async def status(session, job_id, states=("succeeded",)):
    with anyio.fail_after(60):
        while True:
            result = await call(session, "job_status", {"job_id": job_id})
            if result["state"] in states:
                return result
            assert result["state"] not in {"failed", "cancelled", "blocked"}, result
            await anyio.sleep(0.02)


class Lab(SimpleNamespace):
    @asynccontextmanager
    async def client(self, *, configured=False, **changes):
        number = self.clients
        self.clients += 1
        marker = self.root / f"server-{number}.json"
        log = self.root / f"server-{number}.log"
        env = self.env | {"INTEGRATION_SERVER": str(marker)} | changes
        config = (
            json.loads((REPO / ".mcp.json").read_text())["mcpServers"]["sonoranlab"]
            if configured
            else {"command": str(REPO / ".venv/bin/sonoranlab"), "args": ["mcp", "serve"]}
        )
        params = StdioServerParameters(**config, env=env, cwd=REPO)
        host = SimpleNamespace(killed=False, marker=marker, log=log)
        with anyio.fail_after(90), log.open("w+") as errlog:
            try:
                async with stdio_client(params, errlog=errlog) as streams, ClientSession(*streams) as session:
                    initialized = await session.initialize()
                    assert initialized.server_info.name == "sonoranlab"
                    await wait_file(marker)
                    metadata = json.loads(marker.read_text())
                    assert metadata["heavy"] == []
                    host.pid = metadata["pid"]
                    yield session, host
            except ExceptionGroup:
                if not host.killed:
                    raise
        assert "SonoranLab stdio server ready" in log.read_text()

    def output(self, job_id):
        return self.state / json.loads((self.state / "jobs" / job_id / "job.json").read_text())["output_dir"]

    def owner_cli(self, *args):
        result = subprocess.run(
            [str(REPO / ".venv/bin/sonoranlab"), *args],
            env=self.env,
            cwd=REPO,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    async def references(self, session):
        result = await call(session, "register_character", self.base | {"reference_dir": str(self.stills)})
        assert result["approval"]["state"] == "unapproved"
        ref = result["ref_set_id"]
        # Explicitly authorized task fixture: approval applies only to synthetic tmp_path art.
        self.owner_cli(
            "registry",
            "approve-references",
            "--project",
            "toy",
            "--character",
            "hero",
            "--ref-set",
            ref,
            "--reviewer",
            "fixture owner",
            "--evidence",
            "synthetic fixture inspection",
        )
        return self.base | {"ref_set_id": ref, "anim": "idle", "seeds": [1], "godot_check": False}


@pytest.fixture
def lab(tmp_path):
    profile = toy_profile(tmp_path)
    repo = tmp_path / "repo"
    (repo / "profiles").mkdir(parents=True)
    data = yaml.safe_load(profile.read_text())
    data["palette"]["source"] = str(tmp_path / "palette.png")
    data["export"]["godot_project"] = str(tmp_path / "game")
    data["animations"]["idle"]["track_default"] = "E"
    (repo / "profiles/toy.yaml").write_text(yaml.safe_dump(data))
    shutil.copytree(REPO / "workflows", repo / "workflows")
    (repo / "env").mkdir()
    write_birefnet(tmp_path / "work", repo / "env/models.tsv")  # synthetic DYL-125 BiRefNet files
    template = repo / "templates/poses/idle/east/pose_000.png"
    template.parent.mkdir(parents=True)
    shutil.copyfile(REPO / "templates/poses/run/east/pose_000.png", template)
    hook = tmp_path / "hooks"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(SITECUSTOMIZE)
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "uv").symlink_to(shutil.which("uv"))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SONORANLAB_", "INTEGRATION_"))}
    env.update(
        SONORANLAB_STATE=str(tmp_path / "state"),
        SONORANLAB_WORK=str(tmp_path / "work"),
        SONORANLAB_READ_ROOTS=str(tmp_path),
        SONORANLAB_JOB_LAUNCHER="popen",
        SONORANLAB_JOB_EXECUTORS="integration_fakes",
        SONORANLAB_EDITOR_RUNTIME=str(tmp_path / "missing-runtime"),
        INTEGRATION_REPO=str(repo),
        INTEGRATION_MODE="gate",
        INTEGRATION_OFFLINE="1",
        PYTHONPATH=os.pathsep.join(map(str, (hook, HERE, HERE.parent / "jobs", HERE.parent, REPO))),
        PATH=str(binary),
        UV_OFFLINE="1",
        UV_CACHE_DIR=str(tmp_path / "uv-cache"),
    )
    instance = Lab(
        root=tmp_path,
        repo=repo,
        state=tmp_path / "state",
        env=env,
        clients=0,
        base={"project": "toy", "character": "hero"},
        stills=stills_dir(tmp_path),
        raw=raw_set(tmp_path),
    )
    yield instance
    # No production APIs in teardown: kill only verified fixture worker process groups.
    store = importlib.import_module("sonoranlab.jobs.store")
    for path in instance.state.glob("jobs/job-*"):
        if store.live(path):
            job = json.loads((path / "job.json").read_text())
            pid = job["worker"]["pid"]
            if pid and os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
