"""Campaign ledger fixtures: the DYL-116 toy generation setup (real animate pipeline, fake generation runner, no GPU)
and the DYL-128 class-request fixtures. Reuses both subtrees' modules and the DYL-113 stdio harness."""

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "mcp"), str(ROOT / "generation"), str(ROOT / "classreq")]

import test_generation  # noqa: E402
from classreq_fixtures import request  # noqa: E402

from sonoranlab import campaign  # noqa: E402
from sonoranlab.api import call  # noqa: E402
from sonoranlab.api.tools import classreq as classreq_tool  # noqa: E402

setup = test_generation.setup
boundary = importlib.import_module("sonoranlab.api.registry")  # the package re-exports a `registry` function


@pytest.fixture
def lab(setup, monkeypatch):
    """The toy hero (approved references, idle = Track E). Its 24 px toy cell is below the class-request cell range,
    so admission's still check is stubbed to `ready` here; real admission is covered with the DYL-128 fixtures."""
    ctx, animate, _, approval = setup
    monkeypatch.setattr(campaign, "ANIMATE_OPTIONS", {"godot_check": False})
    monkeypatch.setattr(boundary, "Context", lambda *a, **k: ctx)  # the owner CLI builds its own Context()
    monkeypatch.setattr(classreq_tool, "check_class_request", lambda *a, **k: {"status": "ready", "findings": []})
    return {"ctx": ctx, "ref": animate["ref_set_id"], "approval": approval, "request": request(clips=["idle"])}


def api(lab, tool, **params):
    return call(tool, params, ctx=lab["ctx"])


def ok(envelope):
    assert envelope["ok"], envelope
    return envelope["result"]


def create(lab, **extra):
    result = ok(api(lab, "campaign_create", project="toy", request=lab["request"], reference_id=lab["ref"], **extra))
    assert result["created"], result
    return result["campaign_id"]
