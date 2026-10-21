"""Use DYL-113's real stdio harness for all registry tools and owner-only exclusion."""

import json
import os
import subprocess
import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client
from test_review import job as real_job
from test_review import template as real_template
from test_stdio import BOOTSTRAP, REPO, run_async

from sonoranlab.api import registry
from sonoranlab.artifacts import sha256_file

job = real_job
template = real_template


# The subprocess transports use a repository-owned profile copy identifying the fixture's project.
def test_registry_stdio_cli_parity(job, tmp_path):
    profiles = tmp_path / "repo/profiles"
    profiles.mkdir(parents=True)
    manifest = json.loads((job / "manifest.json").read_text())
    (profiles / "toy.yaml").write_bytes(Path(manifest["profile"]["path"]).read_bytes())
    refs = tmp_path / "refs"
    refs.mkdir()
    from PIL import Image
    from test_pipeline import CELL, ORDER

    for direction in ORDER:
        Image.new("RGBA", (CELL, CELL), (1, 2, 3, 255)).save(refs / f"still_{direction}.png")
    patch_context = (
        "\nfrom pathlib import Path\n"
        "import sonoranlab.api.registry as ignored\n"
        "import importlib\n"
        "boundary = importlib.import_module('sonoranlab.api.registry')\n"
        "original_context = boundary.Context\n"
        f"boundary.Context = lambda: original_context(Path({str(profiles.parent)!r}))\n"
        "from sonoranlab.api import paths\npaths.Context = boundary.Context\n"
    )
    cli_bootstrap = patch_context + "\nimport runpy; runpy.run_module('sonoranlab.cli', run_name='__main__')\n"
    env = dict(
        os.environ,
        SONORANLAB_STATE=str(tmp_path / "state"),
        SONORANLAB_READ_ROOTS=str(tmp_path.parent),
        SONORANLAB_EDITOR_RUNTIME=str(tmp_path / "missing"),
    )

    def cli(name, params):
        proc = subprocess.run(
            [sys.executable, "-c", cli_bootstrap, "api", name, "--params", "-"],
            input=json.dumps(params),
            text=True,
            capture_output=True,
            env=env,
            cwd=REPO,
            timeout=20,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return json.loads(proc.stdout)

    registration = {"project": "toy", "character": "hero", "reference_dir": str(refs)}
    importing = {"project": "toy", "character": "hero", "job_dir": str(job)}
    cli("register_character", registration)
    imported = cli("import_job", importing)["result"]
    cases = {
        "list_characters": {"project": "toy"},
        "get_character": {"project": "toy", "character": "hero"},
        "register_character": registration,
        "import_job": importing,
        "get_artifact": {"ref": imported["job_id"] + ":report.json"},
        "check_review": {"job": imported["job_id"]},
    }

    async def exercise():
        params = StdioServerParameters(
            command=sys.executable, args=["-c", patch_context + BOOTSTRAP], env=env, cwd=REPO
        )
        with anyio.fail_after(90), (tmp_path / "stderr.log").open("w+") as errlog:
            async with stdio_client(params, errlog=errlog) as streams, ClientSession(*streams) as session:
                await session.initialize()
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                assert not any("approve" in name for name in tools)
                for name, arguments in cases.items():
                    tool = registry()[name]
                    assert tools[name].input_schema == tool.input_schema
                    assert tools[name].annotations.read_only_hint == tool.read_only
                    result = await session.call_tool(name, arguments)
                    assert not result.is_error, result
                    assert result.structured_content == cli(name, arguments)
                    assert json.loads(result.content[0].text) == result.structured_content
                assert tools["import_job"].annotations.read_only_hint is False
                assert (await session.call_tool("approve_references", {})).is_error

    before = sha256_file(job / "report.json")
    run_async(exercise)
    assert sha256_file(job / "report.json") == before
