"""Real stdio MCP server vs the CLI for campaign_status/campaign_submit (jobs are never launched: no GPU)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import anyio
from conftest import create
from mcp import ClientSession, StdioServerParameters, stdio_client
from test_stdio import BOOTSTRAP, run_async

from sonoranlab.api.tools import campaign as tools
from sonoranlab.jobs import manager

REPO = Path(__file__).resolve().parents[3]


def test_mcp_cli_campaign_status_submit_parity(lab, tmp_path):
    cid = create(lab)
    patch = (
        "from pathlib import Path\nimport importlib\n"
        "boundary=importlib.import_module('sonoranlab.api.registry')\n"
        f"ctx=boundary.Context(Path({str(lab['ctx'].repo)!r}))\n"
        "boundary.Context=lambda *a, **k:ctx\n"
        "from sonoranlab.api import paths\npaths.Context=lambda:ctx\n"
        "from sonoranlab.jobs import manager\n"
        "manager.launch=lambda job:None\nmanager._launcher=lambda:'popen'\n"
        "from sonoranlab.jobs.kinds import generation\ngeneration.Context=lambda:ctx\n"
    )
    env = dict(os.environ)

    def cli(*argv):
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                patch + "\nimport runpy; runpy.run_module('sonoranlab.cli',run_name='__main__')",
                *argv,
            ],
            text=True,
            capture_output=True,
            timeout=30,
            cwd=REPO,
            env=env,
        )
        return proc.returncode, json.loads(proc.stdout)

    async def exercise():
        parameters = StdioServerParameters(command=sys.executable, args=["-c", patch + BOOTSTRAP], env=env, cwd=REPO)
        with anyio.fail_after(90), (tmp_path / "mcp.log").open("w+") as log:
            async with stdio_client(parameters, errlog=log) as streams, ClientSession(*streams) as session:
                await session.initialize()
                advertised = {t.name: t for t in (await session.list_tools()).tools}
                assert advertised["campaign_status"].input_schema == tools.STATUS_SCHEMA
                assert advertised["campaign_submit"].input_schema == tools.SUBMIT_SCHEMA
                assert not [name for name in advertised if "owner" in name]
                status = await session.call_tool("campaign_status", {"campaign_id": cid})
                assert status.structured_content["result"]["state"] == "READY"
                assert (0, status.structured_content) == cli("campaign", "status", "--campaign", cid)
                submitted = await session.call_tool("campaign_submit", {"campaign_id": cid, "clip": "idle"})
                assert not submitted.is_error, submitted
                result = submitted.structured_content["result"]
                assert json.loads(submitted.content[0].text) == submitted.structured_content
                assert result["seed"] == 0 and manager.get(result["job_id"])["track"] == "E"
                status = await session.call_tool("campaign_status", {"campaign_id": cid})
                assert status.structured_content["result"]["state"] == "GENERATING"
                assert (0, status.structured_content) == cli("campaign", "status", "--campaign", cid)
                again = await session.call_tool("campaign_submit", {"campaign_id": cid, "clip": "idle"})
                assert again.is_error
                code, printed = cli("campaign", "submit", "--campaign", cid, "--clip", "idle")
                assert code == 1 and printed == again.structured_content
                assert printed["error"]["message"].startswith("GENERATING")

    run_async(exercise)
    assert len(manager.reconcile_all()) == 1
