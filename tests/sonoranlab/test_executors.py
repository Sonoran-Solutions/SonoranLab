"""The test-only executor override must not outlive the environment that named it (it leaked across test dirs)."""

import sys
import types

from sonoranlab.jobs import executors


def test_override_is_per_call_and_never_cached(monkeypatch):
    real = executors.executor("smoke_render")
    fake = object()
    monkeypatch.setitem(sys.modules, "fake_executors_mod", types.SimpleNamespace(EXECUTORS={"smoke_render": fake}))
    monkeypatch.setenv("SONORANLAB_JOB_EXECUTORS", "fake_executors_mod")
    assert executors.executor("smoke_render") is fake
    assert executors.executor("godot_check") is executors.EXECUTORS["godot_check"]  # not overridden: the real one
    monkeypatch.delenv("SONORANLAB_JOB_EXECUTORS")
    assert executors.executor("smoke_render") is real and executors.EXECUTORS["smoke_render"] is real
