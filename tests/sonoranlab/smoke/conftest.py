import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import synth


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """Roots: tmp_path is the read root; profile, jobs and staged sheets live under it."""
    monkeypatch.setenv("SONORANLAB_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", str(tmp_path))
    work = tmp_path / "work"
    work.mkdir()
    profile = synth.toy_profile(work)
    return type("Lab", (), {"root": tmp_path, "work": work, "profile": profile})
