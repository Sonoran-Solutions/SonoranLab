"""DYL-129 x DYL-130: the owner's single review of the composed sheet drives campaign state; bundles add only
STAGED/READY, never INSTALLED."""

from pathlib import Path

import pytest
from test_newclass import PASS, approved_ref, invoke, ok

from sonoranlab import campaign
from sonoranlab.api import call, paths
from sonoranlab.api.registry import ToolError
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
    request_sha = campaign._view(campaign._find(cid), lab.ctx)["_revisions"][-1]["request_sha256"]
    job = {"kind": "build_delivery", "state": "succeeded", "job_id": "b", "params": {"campaign_id": cid}}
    job["params"]["composed"] = a.composed["job"]
    job["input_hashes"] = {"request": request_sha}
    assert campaign._bundle_state(cid, {a.composed["job"]}, request_sha, [job], lab.ctx) == "STAGED_AND_ENGINE_VERIFIED"
    assert campaign._bundle_state(cid, {a.composed["job"]}, "0" * 64, [job], lab.ctx) is None  # another request
    argv = ["campaign", "owner-decision", "--campaign", cid, "--kind", "delivery", "--ref", "b1"]
    assert main([*argv, "--state", "installed", "--verbatim", "not authorized"]) == 1
    capsys.readouterr()
    assert status(cid)["state"] == "ARTIFACT_APPROVED"


# ---------------------------------------------------------------- senior review P1: delivery binds the campaign


def _approved(lab):
    cid, ref, composed, a = composed_lab(lab)
    record_review(composed, **PASS)
    assert status(cid)["state"] == "ARTIFACT_APPROVED"
    return cid, ref, composed, a


def _delivery_params(cid, composed_job, rev="0" * 40):
    return {
        "project": "toy",
        "campaign_id": cid,
        "composed": composed_job,
        "target_rev": rev,
        "base_manifest": "game/characters.json",
        "import_template": "game/knight.png.import",
    }


def _revise(lab, cid, ref, **request):
    return ok(
        call(
            "campaign_create",
            {"project": "toy", "request": lab.request | request, "reference_id": ref, "revise": cid},
        )
    )


def frozen_ref(cid):
    return campaign._load(campaign._find(cid))[1][-1]["references"]["ref_set_id"]


def test_delivery_accepts_the_current_approved_sheet_only(lab):
    cid, _, _, a = _approved(lab)
    assert campaign.check_delivery(cid, a.composed["job"], lab.ctx)["revision"] == 1
    unknown = call("build_delivery", _delivery_params(cid, "job-20261028T000000Z-00000000"))
    assert unknown["error"]["code"] in {"not_found", "refused"}


def test_delivery_refuses_an_older_revisions_sheet(lab):
    cid, ref, _, a = _approved(lab)
    _revise(lab, cid, ref, display_name="Hero v2")  # request changed: the approved sheet belongs to revision 1
    refused = call("build_delivery", _delivery_params(cid, a.composed["job"]))
    assert refused["error"]["code"] == "refused" and "current revision" in refused["error"]["message"]


def test_delivery_refuses_after_the_references_change(lab):
    import numpy as np
    from conftest import approve
    from PIL import Image
    from test_newclass import register

    cid, _, _, a = _approved(lab)
    stills = lab.root / "stills-v2"
    stills.mkdir()
    for p in lab.stills.iterdir():
        (stills / p.name).write_bytes(p.read_bytes())
    still = stills / "still_south.png"
    px = np.asarray(Image.open(still).convert("RGBA")).copy()
    ys, xs = np.nonzero(px[..., 3])
    px[ys[0], xs[0]] = 0  # one opaque pixel becomes transparent: a different, still binary-alpha reference
    Image.fromarray(px).save(still)
    ref2 = register(lab, stills)
    assert ref2 != frozen_ref(cid)
    approve(ref2, stills)
    ok(call("campaign_create", {"project": "toy", "request": lab.request, "reference_id": ref2, "revise": cid}))
    refused = call("build_delivery", _delivery_params(cid, a.composed["job"]))
    assert refused["error"]["code"] == "refused", refused


def test_delivery_refuses_a_sheet_composed_outside_this_campaign(lab):
    cid, ref, _, _ = _approved(lab)
    selected = {c: v["selected"] for c, v in status(cid)["clips"].items()}
    params = {"project": "toy", "character": "hero", "reference_id": ref}
    outside = ok(call("compose_sheet", params | {"clips": [{"name": c, "job": selected[c]} for c in ("idle", "run")]}))
    from sonoranlab.jobs import manager, store, worker

    worker._work(store.job_dir(outside["job_id"]))
    # approved by the owner, but not composed in this campaign
    record_review(store.output(manager.get(outside["job_id"])) / "composed", **PASS)
    refused = call("build_delivery", _delivery_params(cid, outside["job_id"]))
    assert refused["error"]["code"] == "refused", refused
    with pytest.raises(ToolError, match="current revision"):
        campaign.check_delivery(cid, outside["job_id"], lab.ctx)


def test_height_only_revision_keeps_the_approved_sheet_deliverable(lab):
    cid, ref, _, a = _approved(lab)
    _revise(lab, cid, ref, height_m=lab.request["height_m"] + 0.04)
    assert campaign.check_delivery(cid, a.composed["job"], lab.ctx)["revision"] == 2


def test_delivery_binding_is_rechecked_at_execution(lab):
    from test_newclass import game_repo

    from sonoranlab.jobs import manager, store, worker
    from sonoranlab.jobs.executors import executor

    cid, ref, composed, a = _approved(lab)
    _, rev = game_repo(lab, composed / "export/hero/hero.png")
    job_id = ok(call("build_delivery", _delivery_params(cid, a.composed["job"], rev)))["job_id"]  # admitted
    _revise(lab, cid, ref, display_name="Hero v3")  # the approval no longer matches the campaign
    worker._work(store.job_dir(job_id))
    job = manager.get(job_id)
    assert job["state"] == "failed" and job["error"]["stage"] == "prepare"
    with pytest.raises(ToolError, match="current revision"):  # what run() hit: its first step re-validates
        executor("build_delivery").validate(job["params"])
    assert not (store.output(job) / "bundle/bundle.json").exists()


def test_campaign_cannot_change_while_a_bundle_is_built(lab):
    cid, ref, _, _ = _approved(lab)
    with campaign.delivery_hold(cid):  # held by BuildDelivery.run from the authorization check to the bundle
        busy = _revise_raw(lab, cid, ref, display_name="Hero v4")
        assert busy["error"]["code"] == "conflict" and "campaign_busy" in busy["error"]["message"]
    assert ok(_revise_raw(lab, cid, ref, display_name="Hero v4"))["revision"] == 2  # released afterwards


def _revise_raw(lab, cid, ref, **request):
    params = {"project": "toy", "request": lab.request | request, "reference_id": ref, "revise": cid}
    return call("campaign_create", params)


def test_readiness_ignores_bundles_of_superseded_selections(lab, monkeypatch):
    from sonoranlab.api.tools import delivery

    cid, _, _, a = _approved(lab)
    request_sha = campaign._view(campaign._find(cid), lab.ctx)["_revisions"][-1]["request_sha256"]
    real = campaign._composed_approvals
    old = {"idle": "job-20261028T000000Z-0000000a", "run": "job-20261028T000000Z-0000000b"}  # earlier selections

    def with_old_sheet(*args, **kw):
        return real(*args, **kw) | {"job-old-sheet": old}

    monkeypatch.setattr(campaign, "_composed_approvals", with_old_sheet)
    ready = {"bundles": [{"states": {"READY_TO_INSTALL": True}}]}
    monkeypatch.setattr(delivery, "delivery_status", lambda **k: ready)
    bundle = {
        "kind": "build_delivery",
        "state": "succeeded",
        "job_id": "b-old",
        "input_hashes": {"request": request_sha},
    }
    bundle["params"] = {"campaign_id": cid, "composed": "job-old-sheet"}
    real_jobs = campaign.manager.reconcile_all
    monkeypatch.setattr(campaign.manager, "reconcile_all", lambda: [*real_jobs(), bundle])
    assert status(cid)["state"] == "ARTIFACT_APPROVED"  # the old sheet's bundle does not make this READY
    bundle["params"]["composed"] = a.composed["job"]  # a bundle of the current sheet does
    assert status(cid)["state"] == "READY_TO_INSTALL"
