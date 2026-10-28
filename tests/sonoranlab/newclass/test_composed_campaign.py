"""DYL-129 x DYL-130: the owner's single review of the composed sheet drives campaign state; bundles add only
STAGED/READY, never INSTALLED."""

from pathlib import Path

from test_newclass import PASS, approved_ref, invoke, ok

from sonoranlab import campaign
from sonoranlab.api import call, paths
from sonoranlab.cli import main
from sonoranlab.review import record_review


def composed_lab(lab):
    ref = approved_ref(lab)
    handoff, a = invoke(lab, reference_id=ref)
    assert handoff["stop"] == "AWAITING_OWNER_REVIEW", handoff
    cid = handoff["campaign_id"]
    review_dir = handoff["detail"]["summary"]["owner_review_dir"]
    composed = Path(review_dir.replace("$SONORANLAB_STATE", str(paths.state_root())))
    return cid, ref, composed, a


def status(cid):
    return ok(call("campaign_status", {"campaign_id": cid}))


def compose(cid, ref, clips):
    params = {"project": "toy", "character": "hero", "clips": clips, "reference_id": ref, "campaign_id": cid}
    return call("compose_sheet", params)


def test_composed_event_recorded_and_review_flips_campaign(lab):
    cid, _, composed, a = composed_lab(lab)
    events = [e["data"] for e in campaign.read_events(campaign._find(cid))[0] if e["type"] == "composed"]
    assert [e["job_id"] for e in events] == [a.composed["job"]]
    assert set(events[0]["inputs"]) == {"idle", "run"} and events[0]["revision"] == 1
    assert status(cid)["state"] == "AWAITING_OWNER_REVIEW"
    record_review(composed, **PASS)
    assert status(cid)["state"] == "ARTIFACT_APPROVED"


def test_non_selected_and_old_revision_jobs_are_refused(lab, capsys):
    cid, ref, _, _ = composed_lab(lab)
    selected = {c: v["selected"] for c, v in status(cid)["clips"].items()}
    swapped = compose(cid, ref, [{"name": "idle", "job": selected["run"]}, {"name": "run", "job": selected["idle"]}])
    assert swapped["error"]["code"] == "refused" and "selected candidate" in swapped["error"]["message"]
    revised = ok(
        call(
            "campaign_create",
            {
                "project": "toy",
                "request": lab.request | {"display_name": "Hero v2"},
                "reference_id": ref,
                "revise": cid,
            },
        )
    )
    assert revised["revision"] == 2
    old = compose(cid, ref, [{"name": c, "job": selected[c]} for c in ("idle", "run")])
    assert old["error"]["code"] == "refused"
    assert status(cid)["clips"]["idle"]["selected"] is None


def test_tampered_composed_sheet_is_not_approved(lab):
    cid, _, composed, _ = composed_lab(lab)
    record_review(composed, **PASS)
    assert status(cid)["state"] == "ARTIFACT_APPROVED"
    sheet = composed / "export/hero/hero.png"
    sheet.chmod(0o644)
    sheet.write_bytes(sheet.read_bytes() + b"\0")
    view = status(cid)
    assert view["state"] == "AWAITING_OWNER_REVIEW"
    assert {v["state"] for v in view["clips"].values()} == {"AWAITING_OWNER_REVIEW"}


def test_installed_is_never_derived_from_a_bundle(lab, capsys, monkeypatch):
    from sonoranlab.api.tools import delivery

    cid, _, composed, a = composed_lab(lab)
    record_review(composed, **PASS)
    forged = {"bundles": [{"states": {"INSTALLED": True, "STAGED_AND_ENGINE_VERIFIED": True}}]}
    monkeypatch.setattr(delivery, "delivery_status", lambda **k: forged)
    job = {"kind": "build_delivery", "state": "succeeded", "job_id": "b", "params": {"campaign_id": cid}}
    job["params"]["composed"] = a.composed["job"]
    assert campaign._bundle_state(cid, {a.composed["job"]}, [job], lab.ctx) == "STAGED_AND_ENGINE_VERIFIED"
    argv = ["campaign", "owner-decision", "--campaign", cid, "--kind", "delivery", "--ref", "b1"]
    assert main([*argv, "--state", "installed", "--verbatim", "not authorized"]) == 1
    capsys.readouterr()
    assert status(cid)["state"] == "ARTIFACT_APPROVED"
