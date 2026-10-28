"""The real thing: synthetic sheets through the real Godot 4.7 and the private headless compositor (llvmpipe).
Skipped unless SONORANLAB_SMOKE_REAL=1, because it is a heavy Godot job (one per host; take the host's lock first):

  SONORANLAB_SMOKE_REAL=1 timeout 900 uv run pytest -q -s tests/sonoranlab/smoke/test_real_engine.py

Optional: SONORANLAB_SMOKE_SHADER=<the owner's staged PixelStandee.gdshader> draws the sticker border;
SONORANLAB_SMOKE_OUT=<new dir> keeps the output (default: pytest tmp dir, printed)."""

import json
import os
from pathlib import Path

import pytest
import synth

from sonoranlab import smoke

pytestmark = pytest.mark.skipif(os.environ.get("SONORANLAB_SMOKE_REAL") != "1", reason="heavy real-Godot job")


def test_real_render_of_synthetic_sheets(lab, tmp_path, monkeypatch):
    shader = os.environ.get("SONORANLAB_SMOKE_SHADER")
    if shader:
        monkeypatch.setenv("SONORANLAB_READ_ROOTS", os.pathsep.join([str(tmp_path), str(Path(shader).parent)]))
    out = Path(os.environ.get("SONORANLAB_SMOKE_OUT") or tmp_path / "real")
    cand = synth.export_job(lab.work, "hero")
    sibs = {s: synth.export_job(lab.work, s) for s in ("ally", "boss", "kin")}
    doc = smoke.run_smoke(
        profile=lab.profile,
        candidate=cand,
        siblings=sibs,
        out=out,
        shader=Path(shader) if shader else None,
        timeout=600,
    )
    print(json.dumps({k: doc[k] for k in ("status", "engine", "godot", "border", "supervision")}, indent=1))
    print("failed checks:", {k: v for k, v in doc["checks"].items() if not v["pass"]}, "->", out / "smoke.json")
    assert doc["exit"] == 0 and doc["status"] == "pass"
