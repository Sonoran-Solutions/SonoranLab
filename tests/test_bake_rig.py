"""Track D bake: two runs of tools/godot/bake.sh on the same inputs give identical joints and PNG bytes,
and every clip's lowest opaque row sits on the foot row. Skips when Godot, the headless compositor or
the CC0 rig (see templates/poses/run/template.yaml for its source) is not on this machine."""

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GODOT_DIR = REPO / "tools" / "godot"
RIG = Path(
    os.environ.get("SL_RIG_DIR", Path.home() / "sonoranlab-work/rigs/quaternius")
)
GODOT = Path(os.environ.get("GODOT", Path.home() / ".local/bin/godot"))

spec = importlib.util.spec_from_file_location("check_bake", GODOT_DIR / "check_bake.py")
check_bake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_bake)


@pytest.mark.skipif(
    not (
        GODOT.exists()
        and shutil.which("gnome-shell")
        and (RIG / "UAL1_Standard.glb").exists()
    ),
    reason="needs Godot 4, gnome-shell and the Quaternius rig",
)
def test_bake_is_reproducible_and_registered(tmp_path: Path) -> None:
    for run in ("a", "b"):
        subprocess.run(
            [GODOT_DIR / "bake.sh", "--", "--rig", RIG / "Superhero_Male_FullBody.gltf",
             "--anim-lib", RIG / "UAL1_Standard.glb", "--frames", "6", "--cells", "136",
             "--dirs", "east,south", "--out", tmp_path / run],
            check=True, timeout=300,
        )  # fmt: skip
    assert check_bake.problems(tmp_path / "a") == []
    assert check_bake.compare(tmp_path / "a", tmp_path / "b") == []
