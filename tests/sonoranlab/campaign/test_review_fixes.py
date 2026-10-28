"""Regressions from the cross-family review of 2e7303b (each failed on that commit)."""

import hashlib
from pathlib import Path

import pytest
from conftest import api, create, ok
from test_animate import Fake
from test_generation import execute

from sonoranlab import campaign
from sonoranlab.cli import main
from sonoranlab.jobs import manager, store


def run(job_id, monkeypatch, runner=None):
    return execute(manager.get(job_id), runner or Fake(), monkeypatch)


def submit(lab, cid, **extra):
    return api(lab, "campaign_submit", campaign_id=cid, clip="idle", **extra)


def status(lab, cid):
    return ok(api(lab, "campaign_status", campaign_id=cid))


def test_retry_job_and_plain_animate_cannot_bypass_the_campaign(lab, monkeypatch):
    cid = create(lab)
    job_id = ok(submit(lab, cid))["job_id"]
    assert run(job_id, monkeypatch, Fake(refuse=(1, 3)))["state"] == "failed"
    retried = api(lab, "retry_job", job_id=job_id, idempotency_key="outside-the-ledger")
    assert retried["error"]["code"] == "refused" and cid in retried["error"]["message"]
    plain = {"project": "toy", "character": "hero", "ref_set_id": lab["ref"], "anim": "idle", "seeds": [0]}
    refused = api(lab, "animate", **plain, godot_check=False)
    assert refused["error"]["code"] == "refused" and "campaign_submit" in refused["error"]["hint"]
    assert len(manager.reconcile_all()) == 1


def test_passing_candidate_does_not_hide_a_spent_budget(lab, monkeypatch):
    cid = create(lab, max_seeds_per_clip=1, budget_decision_ref="O6 fixture")
    run(ok(submit(lab, cid))["job_id"], monkeypatch)
    clip = status(lab, cid)["clips"]["idle"]
    assert clip["state"] == "TECHNICAL_PASS" and clip["budget"]["remaining"] == 0
    refused = submit(lab, cid)
    assert refused["error"]["code"] == "refused" and refused["error"]["message"].startswith("HOLD_BUDGET")
    assert status(lab, cid)["clips"]["idle"]["budget"]["remaining"] == 0


def _revise(lab, cid, name, **extra):
    request = lab["request"] | {"display_name": name}
    return api(lab, "campaign_create", project="toy", request=request, reference_id=lab["ref"], revise=cid, **extra)


def test_revision_carries_the_cap_and_cannot_raise_it(lab):
    cid = create(lab, max_seeds_per_clip=2, budget_decision_ref="O6 fixture")
    raised = _revise(lab, cid, "Hero A", max_seeds_per_clip=5, budget_decision_ref="x")
    assert raised["error"]["code"] == "refused"
    revised = ok(_revise(lab, cid, "Hero B"))
    assert revised["created"] and revised["budget"] == {"idle": 2}
    assert status(lab, cid)["clips"]["idle"]["budget"]["base"] == 2


def test_old_revision_selection_review_and_delivery_do_not_satisfy_a_new_revision(lab, monkeypatch, capsys):
    from test_review import PASS

    from sonoranlab.review import record_review

    cid = create(lab)
    job_id = ok(submit(lab, cid))["job_id"]
    run(job_id, monkeypatch)
    ok(api(lab, "campaign_decide", campaign_id=cid, job_id=job_id, decision="selected", reason="good"))
    record_review(Path(manager.get(job_id)["result"]["animate"]["verdict"]["job"]), **PASS)
    argv = ["campaign", "owner-decision", "--campaign", cid, "--kind", "delivery"]
    assert main([*argv, "--state", "staged_and_engine_verified", "--ref", "b1", "--verbatim", "staged"]) == 0
    capsys.readouterr()
    assert status(lab, cid)["state"] == "STAGED_AND_ENGINE_VERIFIED"
    ok(_revise(lab, cid, "Hero renamed"))
    view = status(lab, cid)
    clip = view["clips"]["idle"]
    assert view["state"] == clip["state"] == "READY"
    assert clip["candidates"] == [] and clip["selected"] is None
    assert clip["budget"]["consumed"] == 1 and clip["attempts"][0]["current"] is False
    old = api(lab, "campaign_decide", campaign_id=cid, job_id=job_id, decision="selected", reason="again")
    assert old["error"]["code"] == "refused"


def test_recovery_refuses_to_adopt_a_job_with_another_seed(lab, monkeypatch):
    cid = create(lab)
    original = manager.submit

    def wrong_then_crash(kind, params, key):
        original(kind, params | {"seeds": [7]}, key)
        raise SystemExit("died after a mismatched submission")

    monkeypatch.setattr(manager, "submit", wrong_then_crash)
    with pytest.raises(SystemExit):
        submit(lab, cid)
    monkeypatch.setattr(manager, "submit", original)
    attempt = status(lab, cid)["clips"]["idle"]["attempts"][0]
    assert attempt["class"] == "unknown" and "seeds" in attempt["detail"]
    refused = submit(lab, cid)
    assert refused["error"]["code"] == "refused" and refused["error"]["message"].startswith("BLOCKED")
    outcome = [e["data"] for e in campaign.read_events(campaign._find(cid))[0] if e["type"] == "submit_outcome"]
    assert outcome[0]["class"] == "unknown" and "adoption refused" in outcome[0]["detail"]
    assert not [e for e in campaign.read_events(campaign._find(cid))[0] if e["type"] == "submitted"]


def test_forged_owner_event_with_valid_chain_is_ignored(lab, capsys):
    cid = create(lab, max_seeds_per_clip=1, budget_decision_ref="O6 fixture")
    path = campaign._find(cid)
    events, info = campaign.read_events(path)
    forged = {
        "seq": len(events) + 1,
        "at": "2026-10-28T00:00:00+00:00",
        "type": "owner_decision",
        "data": {"kind": "budget_grant", "clip": "idle", "seeds": 5, "verbatim": "forged", "ref": None},
        "prev": info["head"],
    }
    line = store.canonical(forged)
    with (path / "events.jsonl").open("ab") as stream:
        stream.write(line + b"\n")
    assert campaign.read_events(path)[1]["head"] == hashlib.sha256(line).hexdigest()  # the chain itself is valid
    view = status(lab, cid)
    assert view["clips"]["idle"]["budget"]["granted"] == 0
    assert view["problems"] == [
        {
            "code": "uncommitted_privileged_event",
            "seq": 2,
            "detail": "owner event not committed by the owner CLI; ignored",
        }
    ]
    ok(api(lab, "campaign_note", campaign_id=cid, text="a later append must not commit the forged event"))
    assert status(lab, cid)["clips"]["idle"]["budget"]["granted"] == 0
    argv = ["campaign", "owner-decision", "--campaign", cid, "--kind", "budget_grant", "--clip", "idle"]
    assert main([*argv, "--seeds", "1", "--verbatim", "real grant"]) == 0
    capsys.readouterr()
    assert status(lab, cid)["clips"]["idle"]["budget"]["granted"] == 1
