"""CLI parity, pixel diffs, confinement and durable deterministic executors."""

import json
import os
import shutil
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image
from test_pipeline import CELL, GEN, ORDER, PALETTE, figure, raw_set, stills_dir, toy_profile

from sonoranlab.api import call, paths
from sonoranlab.api.registry import Context, ToolError
from sonoranlab.api.tools import deterministic as ops
from sonoranlab.cli import main
from sonoranlab.jobs import manager, store, worker
from sonoranlab.jobs.kinds import deterministic as kinds

BASE = {"project": "toy", "character": "hero", "anim": "idle"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    profiles = repo / "profiles"
    profiles.mkdir(parents=True)
    profile = toy_profile(profiles)
    ctx = Context(repo)
    monkeypatch.setenv("SONORANLAB_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", str(tmp_path))
    monkeypatch.setattr(paths, "Context", lambda: ctx)
    monkeypatch.setattr(kinds, "Context", lambda: ctx)
    monkeypatch.setattr(ops, "Context", lambda: ctx)
    frames = tmp_path / "frames"
    frames.mkdir()
    for d in GEN:
        for i in range(2):
            Image.fromarray(figure(d, i, lift=-1)).save(frames / f"{d}_{i}.png")
    stills = stills_dir(tmp_path)
    return SimpleNamespace(root=tmp_path, ctx=ctx, profile=profile, frames=frames, stills=stills)


def invoke(setup, name, **params):
    envelope = call(name, BASE | params, ctx=setup.ctx)
    assert envelope["ok"], envelope
    return envelope["result"]


def validated(setup):
    return invoke(setup, "validate", frames_dir=str(setup.frames), stills=str(setup.stills), final_resolution=True)


def execute(setup, kind, **params):
    monkey = pytest.MonkeyPatch()
    monkey.setattr(manager, "launch", lambda job: None)
    try:
        result = manager.submit(kind, BASE | params)
    finally:
        monkey.undo()
    worker._work(store.job_dir(result["job_id"]))
    job = manager.get(result["job_id"])
    assert job["state"] == "succeeded", job
    assert not (store.output(job) / "review.json").exists()
    return job


def test_validate_contact_cli_parity_and_new_output(setup, capsys):
    result = validated(setup)
    assert result["report"]["pass"]
    assert (
        main(
            [
                "validate",
                str(setup.frames),
                "--profile",
                str(setup.profile),
                "--character",
                "hero",
                "--anim",
                "idle",
                "--stills",
                str(setup.stills),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == result["report"]
    path = ops.input_file(result["ref"], setup.ctx)
    contact = invoke(setup, "contact_sheet", frames_dir=str(setup.frames), report=result["ref"])
    cli_png = setup.root / "cli.png"
    assert main(["contact", str(setup.frames), str(path), str(cli_png), "--cell", str(CELL), "--foot-y", "20"]) == 0
    assert ops.input_file(contact["ref"], setup.ctx).read_bytes() == cli_png.read_bytes()
    assert contact["dimensions"] == list(Image.open(cli_png).size)
    assert validated(setup)["ref"] != result["ref"]
    assert not (path.parent / "review.json").exists()


@pytest.mark.parametrize("defect", ["empty", "unknown", "wrong_size", "registration"])
def test_validate_cli_failures(setup, capsys, defect):
    params = {"frames_dir": str(setup.frames), "stills": str(setup.stills), "final_resolution": True}
    if defect == "empty":
        for path in setup.stills.iterdir():
            path.unlink()
    elif defect == "unknown":
        Image.new("RGBA", (CELL, CELL)).save(setup.stills / "still_moon.png")
    elif defect == "wrong_size":
        Image.new("RGBA", (1, 1)).save(setup.stills / "still_north.png")
    else:
        reg = setup.root / "reg.json"
        reg.write_text('{"north":{"clipped_px":[-1,0]}}')
        params["registration"] = str(reg)
    cli = [
        "validate",
        str(setup.frames),
        "--profile",
        str(setup.profile),
        "--character",
        "hero",
        "--anim",
        "idle",
        "--stills",
        str(setup.stills),
    ]
    if params.get("registration"):
        cli += ["--registration", params["registration"]]
    code = main(cli)
    result = call("validate", BASE | params, ctx=setup.ctx)
    assert (code == 1 and result["ok"] and not result["result"]["report"]["pass"]) or (
        code == 2 and not result["ok"]
    ), result


def test_registration_is_shared_and_not_processed(setup):
    reg = setup.root / "registration.json"
    reg.write_text(json.dumps({"north": {"clipped_px": [0, 1]}}))
    result = invoke(setup, "validate", frames_dir=str(setup.frames), registration=str(reg), final_resolution=True)
    assert not result["report"]["pass"]
    assert result["report"]["registration"]["north"]["clipped_px"] == [0, 1]
    assert np.array_equal(np.asarray(Image.open(setup.frames / "north_0.png")), figure("north", 0, lift=-1))


@pytest.mark.parametrize("change", ["none", "colour", "alpha"])
def test_compare_exact_pixels(setup, change):
    after = setup.root / "after"
    shutil.copytree(setup.frames, after)
    if change != "none":
        path = after / "north_0.png"
        f = np.array(Image.open(path))
        f[7, 9] = (*PALETTE[1], 255) if change == "colour" else (*f[7, 9, :3], 128)
        Image.fromarray(f).save(path)
    result = invoke(setup, "compare", before=str(setup.frames), after=str(after), final_resolution=True)
    assert result["changed_pixels"] == (change != "none")
    frame = result["frames"]["north_0.png"]
    assert frame["coordinates"] == ([] if change == "none" else [[9, 7]])
    assert frame["alpha"]["coordinates"] == ([[9, 7]] if change == "alpha" else [])
    if change == "alpha":
        assert frame["pixel_problems"]["invalid_alpha"]["delta"] == 1
        assert result["verdict"]["changed"]
    for ref in result["contacts"].values():
        assert ops.input_file(ref, setup.ctx).is_file()


@pytest.mark.parametrize("attack", ["traversal", "symlink", "game", "fifo"])
@pytest.mark.parametrize(
    "field", ["frames_dir", "stills", "registration", "raw", "report", "before", "after", "source"]
)
def test_every_input_confined(setup, attack, field):
    if attack == "traversal":
        evil = str(setup.root / "frames" / ".." / "frames")
    elif attack == "symlink":
        evil = setup.root / "escape"
        evil.symlink_to("/etc/passwd")
    elif attack == "game":
        evil = setup.profile.parent / "game"
    else:
        evil = setup.root / "pipe"
        os.mkfifo(evil)
    ref = validated(setup)["ref"]
    cases = {
        "frames_dir": ("validate", {"frames_dir": str(setup.frames), "final_resolution": True}),
        "stills": ("validate", {"frames_dir": str(setup.frames), "final_resolution": True}),
        "registration": ("validate", {"frames_dir": str(setup.frames), "final_resolution": True}),
        "raw": ("process", {"mode": "raw"}),
        "report": ("contact_sheet", {"frames_dir": str(setup.frames), "report": ref}),
        "before": ("compare", {"before": str(setup.frames), "after": str(setup.frames), "final_resolution": True}),
        "after": ("compare", {"before": str(setup.frames), "after": str(setup.frames), "final_resolution": True}),
        "source": ("export_godot", {}),
    }
    tool, params = cases[field]
    result = call(tool, BASE | params | {field: str(evil)}, ctx=setup.ctx)
    assert not result["ok"], result
    assert result["error"]["code"] in {"path_refused", "invalid_params", "refused"}


def test_descendant_fifo_and_symlink_refused(setup):
    for name in ("fifo.png", "link.png"):
        path = setup.frames / name
        if name.startswith("fifo"):
            os.mkfifo(path)
        else:
            path.symlink_to("/etc/passwd")
        with pytest.raises(ToolError, match=r"regular|Symlink|Symlinks"):
            ops.resolve_input_dir(setup.frames, setup.ctx)
        path.unlink()


def test_export_stills_and_tampering(setup):
    ref = validated(setup)["ref"]
    job = execute(setup, "export_godot", source=ref)
    assert job["result"]["technical"]["validator"] == "pass"
    exported = np.array(Image.open(store.output(job) / "hero/hero.png").convert("RGBA"))
    for r, d in enumerate(ORDER):
        assert np.array_equal(
            exported[r * CELL : (r + 1) * CELL, :CELL], np.asarray(Image.open(setup.stills / f"still_{d}.png"))
        )
    assert not list((setup.profile.parent / "game").iterdir())
    path = setup.frames / "north_0.png"
    Image.new("RGBA", (CELL, CELL), (0, 0, 0, 255)).save(path)
    with pytest.raises(ToolError, match=r"digest|match"):
        kinds.EXECUTORS["export_godot"].validate(BASE | {"source": ref})


@pytest.mark.parametrize("tamper", ["bare", "report", "profile", "timing"])
def test_export_refuses_bad_bindings(setup, tamper):
    ref = validated(setup)["ref"]
    path = ops.input_file(ref, setup.ctx)
    report = json.loads(path.read_text())
    if tamper == "bare":
        report = {"pass": True}
    elif tamper == "report":
        report["digests"]["north_0"] = "0" * 64
    elif tamper == "profile":
        setup.profile.write_text(setup.profile.read_text() + "\n# changed\n")
    else:
        report["params"]["animation"] = {"frames": 2, "fps": 99, "name": "idle", "loop": True}
    if tamper != "profile":
        path.write_text(json.dumps(report))
    with pytest.raises(ToolError):
        kinds.EXECUTORS["export_godot"].validate(BASE | {"source": ref})


def test_process_real_pipeline_and_frozen_hashes(setup):
    raw = raw_set(setup.root)
    job = execute(setup, "process", mode="raw", raw=str(raw), stills=str(setup.stills))
    assert kinds.EXECUTORS["process"].heavy_cpu
    assert job["result"]["technical"]["validator"] == "pass"
    assert job["result"]["report"].startswith(job["job_id"] + ":")
    assert (store.output(job) / "manifest.json").is_file()


@pytest.mark.parametrize("verdict", ["pass", "fail", "import_fail", "missing", "transient"])
def test_fake_godot_all_verdicts_and_retry(setup, monkeypatch, verdict):
    exported = execute(setup, "export_godot", source=validated(setup)["ref"])
    shim = setup.root / "bin"
    shim.mkdir()
    script = shim / "godot"
    marker = shim / "attempted"
    script.write_text(f'''#!/bin/sh
if [ "$1" = "--version" ]; then echo fake-godot; exit 0; fi
if [ "$4" = "--import" ]; then
  if [ "{verdict}" = "import_fail" ]; then echo ERROR >&2; exit 1; fi
  if [ "{verdict}" = "transient" ] && [ ! -f "{marker}" ]; then
    echo attempted > "{marker}"
    echo 'ERROR: Parameter "singleton" is null' >&2
    echo 'is_cmdline_mode' >&2
    exit 134
  fi
  exit 0
fi
if [ "{verdict}" = "fail" ]; then echo '{{"pass":false}}'; exit 1; fi
echo '{{"pass":true}}'
''')
    script.chmod(0o755)
    if verdict == "missing":
        script.unlink()
    monkeypatch.setenv("PATH", str(shim))
    job = execute(setup, "godot_check", source=exported["job_id"] + ":hero/hero.png")
    result = job["result"]
    assert result["technical"]["godot"] == {"transient": "pass", "missing": "unavailable", "import_fail": "fail"}.get(
        verdict, verdict
    )
    attempts = result["godot_check"]["godot_attempts"]
    assert len(attempts) == {"missing": 0, "transient": 4}.get(verdict, 3)
    if verdict == "transient":
        assert attempts[1]["transient_signature"] and not attempts[2]["transient_signature"]


def test_output_collision_and_size_caps(setup, monkeypatch):
    monkeypatch.setattr(ops, "uuid4", lambda: SimpleNamespace(hex="a" * 32))

    class Frozen:
        @staticmethod
        def now(tz):
            from datetime import datetime

            return datetime(2026, 1, 1, tzinfo=tz)

    monkeypatch.setattr(ops, "datetime", Frozen)
    validated(setup)
    with pytest.raises(FileExistsError):
        ops.validate(BASE | {"frames_dir": str(setup.frames)}, setup.ctx)
    with pytest.raises(ToolError):
        ops._size(16385, 1)
    with pytest.raises(ToolError):
        ops._size(8192, 8192)


def test_process_cli_parity(setup, capsys):
    raw = raw_set(setup.root)
    job = execute(setup, "process", mode="raw", raw=str(raw), stills=str(setup.stills))
    out = setup.root / "cli-process"
    assert (
        main(
            [
                "process",
                str(raw),
                "--profile",
                str(setup.profile),
                "--character",
                "hero",
                "--anim",
                "idle",
                "--stills",
                str(setup.stills),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert (out / "report.json").read_bytes() == (store.output(job) / "report.json").read_bytes()
    for path in (out / "export").rglob("*"):
        if path.is_file():
            assert path.read_bytes() == (store.output(job) / path.relative_to(out)).read_bytes()


def test_compare_coordinate_cap(setup):
    import yaml

    doc = yaml.safe_load(setup.profile.read_text())
    doc["characters"]["hero"]["cell"] = 64
    setup.profile.write_text(yaml.safe_dump(doc))
    after = setup.root / "after"
    after.mkdir()
    for d in GEN:
        for i in range(2):
            Image.new("RGBA", (64, 64), (*PALETTE[0], 255)).save(setup.frames / f"{d}_{i}.png")
            Image.new("RGBA", (64, 64), (*PALETTE[1], 255)).save(after / f"{d}_{i}.png")
    result = invoke(setup, "compare", before=str(setup.frames), after=str(after), final_resolution=True)
    frame = result["frames"]["north_0.png"]
    assert frame["count"] == 4096 and len(frame["coordinates"]) == 2000 and frame["truncated"]


def test_stdio_parity_and_validate_does_not_block(setup):
    import os
    import subprocess
    import sys
    import time

    import anyio
    from mcp import ClientSession, StdioServerParameters, stdio_client
    from test_stdio import REPO, TIMER, run_async

    patch = (
        "import importlib\nfrom pathlib import Path\n"
        "boundary=importlib.import_module('sonoranlab.api.registry')\n"
        "Original=boundary.Context\n"
        f"boundary.Context=lambda: Original(Path({str(setup.ctx.repo)!r}))\n"
        "from sonoranlab.api import paths\npaths.Context=boundary.Context\n"
    )
    marker = setup.root / "started"
    bootstrap = (
        TIMER
        + patch
        + (
            "from sonoranlab import cli\nimport time\n"
            "original=cli.validate\n"
            "def slow(*a, **kw):\n"
            f"    Path({str(marker)!r}).touch()\n"
            "    time.sleep(1.5)\n    return original(*a, **kw)\n"
            "cli.validate=slow\n"
            "from sonoranlab.mcp.server import main\nmain()\n"
        )
    )
    env = dict(os.environ)
    arguments = BASE | {"frames_dir": str(setup.frames), "stills": str(setup.stills), "final_resolution": True}
    cli = subprocess.run(
        [
            sys.executable,
            "-c",
            patch + "import runpy; runpy.run_module('sonoranlab.cli', run_name='__main__')",
            "api",
            "validate",
            "--params",
            "-",
        ],
        input=json.dumps(arguments),
        env=env,
        cwd=REPO,
        text=True,
        capture_output=True,
        check=True,
    )
    expected = json.loads(cli.stdout)

    async def exercise():
        with anyio.fail_after(20), (setup.root / "stdio.log").open("w+") as log:
            server = StdioServerParameters(command=sys.executable, args=["-c", bootstrap], env=env, cwd=REPO)
            async with stdio_client(server, errlog=log) as streams, ClientSession(*streams) as session:
                await session.initialize()
                results = []

                async def validate():
                    results.append(await session.call_tool("validate", arguments))

                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(validate)
                    while not marker.exists():
                        await anyio.sleep(0.01)
                    started = time.monotonic()
                    caps = await session.call_tool("capabilities", {})
                    assert time.monotonic() - started < 1.2
                    assert caps.structured_content["ok"]
                    assert not results
                result = results[0].structured_content
                assert result["result"]["report"] == expected["result"]["report"]
                assert result["result"]["ref"] != expected["result"]["ref"]
                for tool in ("process", "validate", "contact_sheet", "compare", "export_godot", "godot_check"):
                    malformed = await session.call_tool(tool, {})
                    assert malformed.structured_content == call(tool, {}, ctx=setup.ctx)
                report = result["result"]["ref"]
                for name, params in (
                    ("contact_sheet", {"frames_dir": str(setup.frames), "report": report}),
                    ("compare", {"before": str(setup.frames), "after": str(setup.frames), "final_resolution": True}),
                ):
                    response = await session.call_tool(name, BASE | params)
                    assert response.structured_content["ok"], response
                    if name == "compare":
                        assert response.structured_content["result"]["changed_pixels"] == 0

    run_async(exercise)


@pytest.mark.parametrize("change", ["input", "output"])
def test_worker_refuses_changed_inputs_and_nonempty_output(setup, monkeypatch, change):
    monkeypatch.setattr(manager, "launch", lambda job: None)
    submitted = manager.submit("export_godot", BASE | {"source": validated(setup)["ref"]})
    job = manager.get(submitted["job_id"])
    out = store.output(job)
    if change == "input":
        Image.new("RGBA", (CELL, CELL)).save(setup.frames / "north_0.png")
    else:
        (out / "sentinel").write_text("preserve")
    worker._work(store.job_dir(job["job_id"]))
    assert manager.get(job["job_id"])["state"] == "failed"
    assert not (out / "hero").exists()
    if change == "output":
        assert (out / "sentinel").read_text() == "preserve"


def test_imported_job_and_report_refs(setup):
    from sonoranlab import registry

    raw = raw_set(setup.root)
    processed = execute(setup, "process", raw=str(raw), mode="raw", stills=str(setup.stills))
    original = store.output(processed)
    imported = registry.import_job("toy", "hero", str(original), ctx=setup.ctx)
    for source in (imported["artifact_id"], processed["job_id"] + ":report.json"):
        result = execute(setup, "export_godot", source=source)
        assert (store.output(result) / "hero/hero.png").read_bytes() == (original / "export/hero/hero.png").read_bytes()


def test_process_technical_fail_is_successful_execution(setup):
    raw = raw_set(setup.root)
    (raw / "north_1.png").unlink()
    result = execute(setup, "process", mode="raw", raw=str(raw))
    assert result["result"]["technical"]["validator"] == "fail"
    assert not (store.output(result) / "export").exists()
