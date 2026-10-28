"""DYL-130 fixtures: the toy hero with Track E `run` + `idle`, a real stdio MCP server subprocess (jobs are recorded,
never launched) and an in-process worker that runs queued jobs with fake generation/engine executors (no GPU, no
Godot). Reuses the DYL-116 generation fixtures and the DYL-113 stdio bootstrap."""

import json
import os
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
import yaml
from mcp import ClientSession, StdioServerParameters, stdio_client

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path[:0] = [str(HERE), str(ROOT), *(str(ROOT / d) for d in ("mcp", "generation", "classreq", "deliver"))]

from classreq_fixtures import request  # noqa: E402
from comfy.birefnet_fixture import write_birefnet  # noqa: E402
from newclass_server import toy_check  # noqa: E402
from test_animate import Fake  # noqa: E402
from test_generation import execute  # noqa: E402
from test_pipeline import ORDER, stills_dir, toy_profile  # noqa: E402
from test_stdio import BOOTSTRAP, run_async  # noqa: E402, F401

from sonoranlab import campaign  # noqa: E402
from sonoranlab import registry as records  # noqa: E402
from sonoranlab.api import Context, paths  # noqa: E402
from sonoranlab.api.tools import classreq as classreq_tool  # noqa: E402
from sonoranlab.artifacts import sha256_file  # noqa: E402
from sonoranlab.jobs import manager, store, worker  # noqa: E402
from sonoranlab.jobs.kinds import generation  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
# Server-side twin of the in-process patches (the subprocess builds its own Context()).
PRELUDE = """
import importlib, os, sys
from pathlib import Path
sys.path[:0] = {paths!r}
boundary = importlib.import_module('sonoranlab.api.registry')
ctx = boundary.Context(Path({repo!r}))
boundary.Context = lambda *a, **k: ctx
from sonoranlab.api import paths
paths.Context = lambda: ctx
from sonoranlab.jobs import manager
manager.launch = lambda job: None
manager._launcher = lambda: 'popen'
from sonoranlab.jobs.kinds import generation
generation.Context = lambda: ctx
from sonoranlab import campaign
campaign.ANIMATE_OPTIONS = {{'godot_check': False}}
import newclass_server
newclass_server.install(ctx)
"""


def approve(ref_id: str, stills: Path) -> Path:
    """Fixture owner evidence (synthetic art only), never an agent call: the owner CLI's approval record."""
    approval = paths.state_root() / "registry/toy/hero/approvals" / f"owner-{ref_id}.json"
    approval.parent.mkdir(exist_ok=True)
    approval.write_text(
        json.dumps(
            {
                "schema": "sonoranlab.ref-approval/1",
                "project": "toy",
                "character": "hero",
                "ref_set_id": ref_id,
                "hashes": {d: sha256_file(stills / f"still_{d}.png") for d in ORDER},
                "reviewer": "fixture owner",
                "evidence": "fixture inspection",
                "approved_at": "2026-10-28T00:00:00Z",
            }
        )
    )
    return approval


class Lab(SimpleNamespace):
    @asynccontextmanager
    async def client(self):
        """One real stdio server per agent invocation (a restart is a new client)."""
        number = self.clients
        self.clients += 1
        prelude = PRELUDE.format(paths=[str(HERE), str(ROOT)], repo=str(self.ctx.repo))
        params = StdioServerParameters(
            command=sys.executable, args=["-c", prelude + BOOTSTRAP], env=dict(os.environ), cwd=REPO
        )
        with anyio.fail_after(240), (self.root / f"server-{number}.log").open("w+") as log:
            async with stdio_client(params, errlog=log) as streams, ClientSession(*streams) as session:
                await session.initialize()
                yield session

    def pending(self) -> list[dict]:
        return [j for j in manager.reconcile_all() if j["state"] == "queued"]

    def run_pending(self) -> None:
        """Simulated time passing: the worker runs every queued job in-process (fake generation, fake engine)."""
        for job in self.pending():
            self.executed.append(job["job_id"])
            execute(job, self.runner, self.monkeypatch)

    async def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.run_pending()

    def jobs(self, kind: str = "animate") -> list[dict]:
        return [j for j in manager.reconcile_all() if j["kind"] == kind]


@pytest.fixture
def lab(tmp_path, monkeypatch):
    def edit(doc):
        doc["animations"] = {
            "idle": {"frames": 2, "fps": 5, "loop": True, "track_default": "E"},
            "run": {"frames": 2, "fps": 10, "loop": True, "track_default": "E"},
            "attack": {"frames": 2, "fps": 5, "loop": False, "track_default": "C"},
        }
        doc["palette"]["source"] = str(tmp_path / "palette.png")
        doc["export"]["godot_project"] = str(tmp_path / "game")

    profile = toy_profile(tmp_path, edit)
    repo = tmp_path / "repo"
    (repo / "profiles").mkdir(parents=True)
    shutil.copyfile(profile, repo / "profiles/toy.yaml")
    shutil.copytree(REPO / "workflows", repo / "workflows")
    shutil.copytree(REPO / "templates", repo / "templates")
    (repo / "env").mkdir()
    monkeypatch.setenv("SONORANLAB_WORK", str(tmp_path / "work"))
    write_birefnet(tmp_path / "work", repo / "env/models.tsv")
    ctx = Context(repo)
    boundary = sys.modules["sonoranlab.api.registry"]
    monkeypatch.setattr(boundary, "Context", lambda *a, **k: ctx)
    monkeypatch.setattr(paths, "Context", lambda: ctx)
    monkeypatch.setattr(generation, "Context", lambda: ctx)
    monkeypatch.setattr(manager, "launch", lambda j: None)
    monkeypatch.setattr(manager, "_launcher", lambda: "popen")
    monkeypatch.setattr(campaign, "ANIMATE_OPTIONS", {"godot_check": False})
    monkeypatch.setattr(classreq_tool, "check_class_request", toy_check)
    import newclass_fakes  # noqa: F401  (bind its Context too)

    for name, module in list(sys.modules.items()):
        if (name.startswith("sonoranlab.") or name == "newclass_fakes") and getattr(module, "Context", None) is type(
            ctx
        ):
            monkeypatch.setattr(module, "Context", lambda *a, **k: ctx)
    monkeypatch.setenv("SONORANLAB_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", str(tmp_path))
    monkeypatch.setenv("SONORANLAB_JOB_EXECUTORS", "newclass_fakes")
    monkeypatch.setenv("SONORANLAB_EDITOR_RUNTIME", str(tmp_path / "missing-runtime"))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(HERE), str(ROOT), os.environ.get("PYTHONPATH", "")]))
    monkeypatch.delenv("SONORANLAB_ALLOW_PAUSE_UNITS", raising=False)
    assert yaml.safe_load((repo / "profiles/toy.yaml").read_text())["animations"]["run"]["track_default"] == "E"
    return Lab(
        root=tmp_path,
        ctx=ctx,
        stills=stills_dir(tmp_path),
        runner=Fake(),
        monkeypatch=monkeypatch,
        clients=0,
        executed=[],
        waits=[],
        request=request(),
        records=records,
        worker=worker,
        store=store,
    )
