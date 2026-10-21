"""An edited report and manifest cannot authorize invalid animation pixels."""

import json
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
