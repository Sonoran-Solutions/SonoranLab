"""DYL-127: pose_frame hands the adapter a control image drawn for the character's cell, not the repo PNG."""

import hashlib
import subprocess
from importlib import import_module
from pathlib import Path

import pytest
from PIL import Image
from test_generation import execute, setup, submit  # noqa: F401  (setup is a fixture)

from sonoranlab.jobs import store
from sonoranlab.jobs.kinds import generation

TEMPLATE_YAML = Path(__file__).resolve().parents[3] / "templates/poses/run/template.yaml"


@pytest.mark.parametrize("cell", [136, 144, 176, 184])
def test_adapter_accepts_control_for_every_profile_cell(tmp_path, cell):
    track_c = import_module("sonoranlab.comfy.adapters.track_c")
    path = tmp_path / "control.png"
    path.write_bytes(generation.posetemplate.control_png(TEMPLATE_YAML, "east", 0, cell))
    assert track_c.TrackC._pose_scale(path, cell, ValueError) == 4
    # The shipped 544 px PNG is the defect: only a cell-136 character can use it.
    shipped = TEMPLATE_YAML.parent / "east/pose_000.png"
    with pytest.raises(ValueError, match="square multiple"):
        track_c.TrackC._pose_scale(shipped, 144, ValueError)


def test_pose_job_binds_and_passes_rendered_control(setup, monkeypatch):  # noqa: F811
    job = submit(setup, "pose_frame")
    hashes = job["input_hashes"]
    assert {"template", "template_yaml", "control"} <= set(hashes)
    seen = {}

    def runner(argv, **kwargs):
        request = store.load(Path(argv[argv.index("--request") + 1]))
        control = Path(request["control_image"])
        with Image.open(control) as im:
            seen["size"] = im.size
        seen["sha"] = hashlib.sha256(control.read_bytes()).hexdigest()
        seen["path"] = control
        out = Path(request["out_dir"])
        out.mkdir()
        store.atomic(out / "result.json", {"request": request, "status": "ok", "error": None})
        return subprocess.CompletedProcess(argv, 0)

    done = execute(job, runner, monkeypatch)
    assert done["state"] == "succeeded", done["error"]
    assert seen["sha"] == hashes["control"]
    assert seen["size"][0] == seen["size"][1] and seen["size"][0] % 24 == 0  # toy fixture cell
    assert store.output(done) in seen["path"].parents  # written into the job's own output dir


def test_pose_job_refuses_changed_template_joints(setup, monkeypatch):  # noqa: F811
    job = submit(setup, "pose_frame")
    repo = setup[0].repo
    yaml_path = repo / "templates/poses/idle/template.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("[-0.29, 80.97]", "[-0.29, 70.97]"))
    done = execute(job, lambda argv, **kw: pytest.fail("must not launch"), monkeypatch)
    assert done["state"] == "failed" and done["error"]["code"] == "inputs_changed"
