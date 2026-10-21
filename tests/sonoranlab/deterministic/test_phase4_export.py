"""An edited report and manifest cannot authorize invalid animation pixels."""

import json
import os
from functools import partial

import numpy as np
import pytest
import test_deterministic as fixtures
from mcp import types
from PIL import Image
from test_deterministic import BASE, execute
from test_pipeline import CELL, ORDER, raw_set
from test_stdio import run_async

from sonoranlab import registry
from sonoranlab.api import call
from sonoranlab.artifacts import sha256_file
from sonoranlab.cli import main
from sonoranlab.jobs import store
from sonoranlab.mcp import server
from sonoranlab.validate import frame_digest

setup = fixtures.setup


def test_native_artifact_metadata_and_confinement(setup):
    job = execute(setup, "process", mode="raw", raw=str(raw_set(setup.root)), stills=str(setup.stills))
    root, identifier = store.output(job), job["job_id"]
    for relative in ("export/hero/hero.png", "export/hero/hero.sonoranlab.json"):
        response = call("get_artifact", {"ref": f"{identifier}:{relative}"}, ctx=setup.ctx)
        assert response["ok"], response
        meta = response["result"]
        assert meta["ref"] == f"{identifier}:{relative}"
        assert meta["size"] == (root / relative).stat().st_size
        assert meta["sha256"] == sha256_file(root / relative)
        assert meta["media_type"] == ("image/png" if relative.endswith(".png") else "application/json")
        if relative.endswith(".png"):
            with Image.open(root / relative) as image:
                assert meta["dimensions"] == list(image.size)
    meta = call("get_artifact", {"ref": identifier}, ctx=setup.ctx)["result"]
    files = [p for p in root.rglob("*") if p.is_file()]
    assert meta["file_count"] == len(files) and meta["size"] == sum(p.stat().st_size for p in files)
    (root / "escape").symlink_to(setup.frames)
    os.mkfifo(root / "fifo")
    for relative in ("../report.json", str(setup.profile), "escape/north_0.png", "fifo"):
        response = call("get_artifact", {"ref": f"{identifier}:{relative}"}, ctx=setup.ctx)
        assert response["error"]["code"] == "path_refused", response


def test_native_process_and_export_review_use_own_output(setup):
    from test_review import PASS

    from sonoranlab.review import record_review

    job = execute(setup, "process", mode="raw", raw=str(raw_set(setup.root)), stills=str(setup.stills))
    root, identifier = store.output(job), job["job_id"]
    assert call("check_review", {"job": identifier}, ctx=setup.ctx)["result"]["review_state"] == "none"
    record_review(root, **PASS)
    assert call("check_review", {"job": identifier}, ctx=setup.ctx)["result"]["production_usable"]
    exported = execute(setup, "export_godot", source=identifier + ":report.json")
    review = call("check_review", {"job": exported["job_id"]}, ctx=setup.ctx)["result"]
    assert review == registry._review(store.output(exported), setup.ctx)
    assert not review["production_usable"]
    (root / "export/hero/hero.png").write_bytes(b"changed")
    review = call("check_review", {"job": identifier}, ctx=setup.ctx)["result"]
    assert review["review_state"] == "invalid" and not review["production_usable"]


def test_process_terminal_stage_is_measured_and_persisted(setup):
    job = execute(setup, "process", mode="raw", raw=str(raw_set(setup.root)), stills=str(setup.stills))
    assert job["stage"] == "validate_export"
    status = call("job_status", {"job_id": job["job_id"]}, ctx=setup.ctx)["result"]
    assert status["stage"] == status["progress"]["stage"] == "validate_export"


@pytest.mark.parametrize("transport", ["cli", "mcp"])
def test_forged_alpha_report_and_manifest_refused(setup, monkeypatch, capsys, transport):
    job = execute(setup, "process", mode="raw", raw=str(raw_set(setup.root)), stills=str(setup.stills))
    root = store.output(job)
    png = root / "export/hero/hero.png"
    sheet = np.array(Image.open(png).convert("RGBA"))
    cell = sheet[:CELL, CELL : 2 * CELL]
    y, x = np.argwhere(cell[..., 3] == 255)[0]
    cell[y, x, 3] = 128
    Image.fromarray(sheet).save(png)
    report_path = root / "report.json"
    report = json.loads(report_path.read_text())
    assert report["pass"]
    report["digests"]["north_0"] = frame_digest(cell)
    report_path.write_text(json.dumps(report))
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["outputs"] = {rel: sha256_file(root / rel) for rel in manifest["outputs"]}
    manifest_path.write_text(json.dumps(manifest))
    entry = registry.import_job("toy", "hero", str(root), ctx=setup.ctx)
    out = setup.root / "forged-export"
    if transport == "cli":
        assert (
            main(
                [
                    "export",
                    "godot",
                    str(png),
                    "--profile",
                    str(setup.profile),
                    "--character",
                    "hero",
                    "--cell",
                    str(CELL),
                    "--rows",
                    ",".join(ORDER),
                    "--clip",
                    "idle:1:2:5:loop",
                    "--report",
                    str(report_path),
                    "--out",
                    str(out),
                ]
            )
            == 2
        )
        assert "alpha" in capsys.readouterr().err
    else:
        monkeypatch.setattr(server, "call", partial(call, ctx=setup.ctx))

        async def exercise():
            response = await server.call_tool(
                None,
                types.CallToolRequestParams(name="export_godot", arguments=BASE | {"source": entry["artifact_id"]}),
            )
            assert response.is_error
            envelope = response.structured_content
            assert envelope["error"]["code"] == "refused", envelope
            assert "alpha" in envelope["error"]["message"]

        run_async(exercise)
    assert not out.exists()
    assert len(list((store.directory("jobs")).glob("job-*"))) == 1
