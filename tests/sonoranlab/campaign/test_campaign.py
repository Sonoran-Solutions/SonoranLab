"""Campaign ledger over the real durable animate jobs (fake generation runner, worker run in-process, no GPU)."""

import json
import multiprocessing
import time
from datetime import datetime
from pathlib import Path

import pytest
from conftest import api, create, ok
from test_animate import Fake
from test_generation import execute

from sonoranlab import campaign
from sonoranlab import registry as records
from sonoranlab.api import paths, registry
from sonoranlab.cli import main
from sonoranlab.jobs import manager, store


def status(lab, cid):
    return ok(api(lab, "campaign_status", campaign_id=cid))


def submit(lab, cid, clip="idle", **extra):
    return api(lab, "campaign_submit", campaign_id=cid, clip=clip, **extra)


def run(job_id, monkeypatch, runner=None):
    return execute(manager.get(job_id), runner or Fake(), monkeypatch)


def events(cid):
    return campaign.read_events(campaign._find(cid))[0]


def owner(cid, *argv):
    return main(["campaign", "owner-decision", "--campaign", cid, *argv])


def test_pass_flow_track_e_review_and_delivery(lab, monkeypatch, capsys):
    from test_review import PASS

    from sonoranlab.review import record_review

    cid = create(lab)
    view = status(lab, cid)
    assert view["state"] == "READY" and view["track"] == "E"
    assert view["clips"]["idle"]["budget"] == {
        "base": 12,
        "granted": 0,
        "consumed": 0,
        "remaining": 12,
        "not_consumed": 0,
    }
    assert {"actor": "agent", "action": "campaign_submit", "clip": "idle", "why": "budget remains"} in view[
        "next_actions"
    ]
    first = ok(submit(lab, cid))
    assert first["seed"] == 0 and first["idempotency_key"] == f"campaign/{cid}/r1/idle/seed-0"
    job = manager.get(first["job_id"])
    # No silent fallback: always the Track E animate kind at the guard's unchanged 40 GiB.
    assert (job["kind"], job["track"], job["need_gib"], job["params"]["seeds"]) == ("animate", "E", 40, [0])
    assert status(lab, cid)["state"] == "GENERATING"
    again = submit(lab, cid)
    assert again["error"]["code"] == "conflict" and again["error"]["message"].startswith("GENERATING")
    run(first["job_id"], monkeypatch)
    view = status(lab, cid)
    assert view["state"] == view["clips"]["idle"]["state"] == "TECHNICAL_PASS"
    assert view["clips"]["idle"]["candidates"][0]["job_id"] == first["job_id"]
    decided = ok(api(lab, "campaign_decide", campaign_id=cid, job_id=first["job_id"], decision="selected", reason="ok"))
    assert decided["clip_state"] == "AWAITING_OWNER_REVIEW"
    assert submit(lab, cid)["error"]["code"] == "refused"
    record_review(Path(manager.get(first["job_id"])["result"]["animate"]["verdict"]["job"]), **PASS)
    assert status(lab, cid)["state"] == "ARTIFACT_APPROVED"
    # Delivery states are owner records; installed needs a prior install authorization for the same bundle.
    assert owner(cid, "--kind", "delivery", "--state", "installed", "--ref", "bundle-1", "--verbatim", "go") == 1
    assert (
        owner(
            cid, "--kind", "delivery", "--state", "staged_and_engine_verified", "--ref", "bundle-1", "--verbatim", "x"
        )
        == 0
    )
    assert status(lab, cid)["state"] == "STAGED_AND_ENGINE_VERIFIED"
    assert owner(cid, "--kind", "install", "--ref", "bundle-1", "--verbatim", "install it") == 0
    assert owner(cid, "--kind", "delivery", "--state", "installed", "--ref", "bundle-1", "--verbatim", "done") == 0
    assert status(lab, cid)["state"] == "INSTALLED"
    capsys.readouterr()
    types = [e["type"] for e in events(cid)]
    assert types[:3] == ["revision", "submitting", "submitted"]
    assert "job_observed" in types and "review_observed" in types


def test_validator_fail_consumes_until_hold_budget_then_owner_grant_adds(lab, monkeypatch, capsys):
    cid = create(lab, max_seeds_per_clip=2, budget_decision_ref="O6 fixture")
    for seed in (0, 1):
        job_id = ok(submit(lab, cid))["job_id"]
        assert manager.get(job_id)["params"]["seeds"] == [seed]
        run(job_id, monkeypatch, Fake(lift={"north": 3}))
    view = status(lab, cid)
    clip = view["clips"]["idle"]
    assert clip["state"] == view["state"] == "HOLD_BUDGET"
    assert clip["budget"] == {"base": 2, "granted": 0, "consumed": 2, "remaining": 0, "not_consumed": 0}
    refused = submit(lab, cid)
    assert refused["error"]["code"] == "refused" and refused["error"]["message"].startswith("HOLD_BUDGET")
    escalated = ok(api(lab, "campaign_escalate", campaign_id=cid, clip="idle", reason="budget spent"))
    assert len(escalated["best_two"]) == 2
    assert all(d["class"] == "validator_fail" and d["verdict"]["failures"] for d in escalated["diagnostics"])
    # A budget grant is owner-only (CLI) and adds; consumption and seeds are never reset.
    assert owner(cid, "--kind", "budget_grant", "--clip", "idle", "--seeds", "1", "--verbatim", "one more") == 0
    capsys.readouterr()
    clip = status(lab, cid)["clips"]["idle"]
    assert clip["state"] == "READY"
    assert clip["budget"] == {"base": 2, "granted": 1, "consumed": 2, "remaining": 1, "not_consumed": 0}
    third = ok(submit(lab, cid))
    assert third["seed"] == 2 and third["budget"]["remaining"] == 0
    assert submit(lab, cid, seed=0)["error"]["code"] in {"conflict", "refused"}


def test_profile_attempt_limit_is_hold_budget(lab, monkeypatch):
    cid = create(lab)
    for _ in range(3):  # toy max_attempts_per_animation = 3 (manager admission), one seed per job
        job = run(ok(submit(lab, cid))["job_id"], monkeypatch, Fake(lift={"north": 3}))
        # The fake runner writes no ComfyUI submission; record the prompt evidence a real attempt leaves.
        gen = store.output(job) / "animate/gen/prompt"
        gen.mkdir(parents=True)
        store.atomic(gen / "submission.json", {"prompt_id": "p-" + job["job_id"]})
        store.atomic(gen / "result.json", {"status": "ok"})
        assert manager.get(job["job_id"])["prompts"]
    clip = status(lab, cid)["clips"]["idle"]
    assert clip["state"] == "HOLD_BUDGET" and clip["attempt_limit"]["used"] == 3
    assert clip["budget"]["remaining"] == 9
    assert submit(lab, cid)["error"]["message"].startswith("HOLD_BUDGET")


def test_guard_refusal_not_consumed_but_blocks_until_owner_resume(lab, monkeypatch, capsys):
    cid = create(lab)
    job_id = ok(submit(lab, cid))["job_id"]
    finished = run(job_id, monkeypatch, Fake(refuse=(1, 3)))
    assert finished["state"] == "failed" and finished["error"]["code"] == "budget_refused"
    view = status(lab, cid)
    clip = view["clips"]["idle"]
    assert view["state"] == clip["state"] == "BLOCKED"
    assert clip["budget"]["consumed"] == 0 and clip["budget"]["not_consumed"] == 1
    assert clip["attempts"][0]["class"] == "guard_refusal"
    refused = submit(lab, cid)
    assert refused["error"]["code"] == "refused" and job_id in refused["error"]["message"]
    assert owner(cid, "--kind", "resume", "--ref", job_id, "--verbatim", "guard was busy; resume") == 0
    capsys.readouterr()
    assert status(lab, cid)["state"] == "READY"
    assert ok(submit(lab, cid))["seed"] == 1  # a refused seed is still never reused


def test_restart_after_intent_before_submit_blocks_without_resubmitting(lab, monkeypatch, capsys):
    cid = create(lab)

    def crash(*args, **kwargs):
        raise SystemExit("orchestrator died before the manager call")

    original = manager.submit
    monkeypatch.setattr(manager, "submit", crash)
    with pytest.raises(SystemExit):
        submit(lab, cid)
    monkeypatch.setattr(manager, "submit", original)
    view = status(lab, cid)
    assert view["state"] == "GENERATING" and view["clips"]["idle"]["attempts"][0]["class"] == "submitting"
    refused = submit(lab, cid)
    assert refused["error"]["code"] == "refused"
    assert not manager.reconcile_all()
    clip = status(lab, cid)["clips"]["idle"]
    assert clip["state"] == "BLOCKED" and clip["attempts"][0]["class"] == "unknown"
    assert clip["budget"]["consumed"] == 1  # consumption counts intents
    key = clip["attempts"][0]["key"]
    assert owner(cid, "--kind", "resume", "--ref", key, "--verbatim", "nothing ran; resume") == 0
    capsys.readouterr()
    assert ok(submit(lab, cid))["seed"] == 1


def test_restart_after_submit_before_job_id_recorded_adopts_the_job(lab, monkeypatch):
    cid = create(lab)
    original = campaign._append

    def crash(path, kind, data):
        if kind == "submitted":
            raise SystemExit("died after the manager returned")
        return original(path, kind, data)

    monkeypatch.setattr(campaign, "_append", crash)
    with pytest.raises(SystemExit):
        submit(lab, cid)
    monkeypatch.setattr(campaign, "_append", original)
    jobs = manager.reconcile_all()
    assert len(jobs) == 1
    view = status(lab, cid)
    assert view["state"] == "GENERATING" and view["clips"]["idle"]["attempts"][0]["job_id"] == jobs[0]["job_id"]
    assert submit(lab, cid)["error"]["message"].startswith("GENERATING")
    adopted = [e["data"] for e in events(cid) if e["type"] == "submitted"]
    assert adopted == [{**adopted[0], "job_id": jobs[0]["job_id"], "adopted": True}]
    assert len(manager.reconcile_all()) == 1


def test_restart_after_job_terminal_before_observed(lab, monkeypatch):
    cid = create(lab)
    job_id = ok(submit(lab, cid))["job_id"]
    run(job_id, monkeypatch)
    assert status(lab, cid)["state"] == "TECHNICAL_PASS"  # derived from the live job store, not from observations
    assert not [e for e in events(cid) if e["type"] == "job_observed"]
    ok(api(lab, "campaign_decide", campaign_id=cid, job_id=job_id, decision="rejected", reason="belt drifts"))
    ok(api(lab, "campaign_note", campaign_id=cid, text="second mutating call"))
    observed = [e["data"] for e in events(cid) if e["type"] == "job_observed"]
    assert observed == [{"job_id": job_id, "state": "succeeded", "class": "pass", "verdict": "pass", "error": None}]
    assert status(lab, cid)["clips"]["idle"]["state"] == "READY"  # the only candidate was rejected


def _racer(cid, ctx_repo, queue):
    from sonoranlab.api import Context, call

    try:
        queue.put(call("campaign_submit", {"campaign_id": cid, "clip": "idle"}, ctx=Context(ctx_repo)))
    except BaseException as exc:
        queue.put({"crash": repr(exc)})


def test_concurrent_submits_one_wins(lab, monkeypatch):
    cid = create(lab)
    original = manager.submit

    def slow(*args, **kwargs):
        time.sleep(0.5)
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "submit", slow)
    fork = multiprocessing.get_context("fork")
    queue = fork.Queue()
    racers = [fork.Process(target=_racer, args=(cid, lab["ctx"].repo, queue)) for _ in range(2)]
    for racer in racers:
        racer.start()
    results = [queue.get(timeout=60) for _ in racers]
    for racer in racers:
        racer.join(10)
    winners = [r for r in results if r.get("ok")]
    losers = [r for r in results if not r.get("ok")]
    assert len(winners) == 1 and len(losers) == 1, results
    message = losers[0]["error"]["message"]
    assert message.startswith(("campaign_busy", "GENERATING")), message
    assert len(manager.reconcile_all()) == 1
    assert [e["type"] for e in events(cid)].count("submitting") == 1


def test_changed_reference_and_profile_are_new_revisions_with_carried_consumption(lab, monkeypatch, tmp_path):
    from test_pipeline import ORDER

    from sonoranlab.artifacts import sha256_file

    cid = create(lab)
    run(ok(submit(lab, cid))["job_id"], monkeypatch, Fake(lift={"north": 3}))
    # A new, owner-approved still set (fixture owner evidence, never an agent approval).
    refs = paths.state_root().parent / "stills"
    changed = tmp_path / "stills-b"
    changed.mkdir()
    for d in ORDER:
        (changed / f"still_{d}.png").write_bytes((refs / f"still_{d}.png").read_bytes())
    from PIL import Image

    img = Image.open(changed / "still_south.png").convert("RGBA")
    img.putpixel((12, 10), (204, 34, 34, 255))
    img.save(changed / "still_south.png")
    new_ref = records.register_character("toy", "hero", str(changed), ctx=lab["ctx"])["ref_set_id"]
    approval = {
        "schema": "sonoranlab.ref-approval/1",
        "project": "toy",
        "character": "hero",
        "ref_set_id": new_ref,
        "hashes": {d: sha256_file(changed / f"still_{d}.png") for d in ORDER},
        "reviewer": "fixture owner",
        "evidence": "fixture inspection",
        "approved_at": "2026-10-21T00:00:00Z",
    }
    (lab["approval"].parent / "owner-b.json").write_text(json.dumps(approval))
    revised = ok(api(lab, "campaign_create", project="toy", request=lab["request"], reference_id=new_ref, revise=cid))
    assert revised["created"] and revised["revision"] == 2 and revised["campaign_id"] == cid
    clip = status(lab, cid)["clips"]["idle"]
    assert clip["budget"]["consumed"] == 1 and clip["seeds_used"] == [0]
    second = ok(submit(lab, cid))
    assert second["seed"] == 1 and second["idempotency_key"] == f"campaign/{cid}/r2/idle/seed-1"
    assert manager.get(second["job_id"])["params"]["ref_set_id"] == new_ref
    run(second["job_id"], monkeypatch, Fake(lift={"north": 3}))
    # A profile edit is never applied mid-campaign: it needs revision 3.
    profile = lab["ctx"].repo / "profiles/toy.yaml"
    profile.write_text(profile.read_text().replace("name: Toy", "name: Toy edited"))
    view = status(lab, cid)
    assert view["state"] == "NEEDS_OWNER_INPUT" and "revision_required" in view["gate"]["detail"]
    assert submit(lab, cid)["error"]["code"] == "refused"
    third = ok(api(lab, "campaign_create", project="toy", request=lab["request"], reference_id=new_ref, revise=cid))
    assert third["revision"] == 3
    view = status(lab, cid)
    assert view["state"] == "READY" and view["clips"]["idle"]["budget"]["consumed"] == 2
    # An unchanged revise writes nothing.
    same = ok(api(lab, "campaign_create", project="toy", request=lab["request"], reference_id=new_ref, revise=cid))
    assert not same["created"] and same["revision"] == 3


def test_stale_approval_awaits_reference_approval(lab):
    cid = create(lab)
    still = lab["approval"].parent.parent / "refs" / lab["ref"] / "still_east.png"
    still.chmod(0o644)
    still.write_bytes(still.read_bytes() + b"\0")
    view = status(lab, cid)
    assert view["state"] == view["gate"]["state"] == "AWAITING_REFERENCE_APPROVAL"
    assert view["next_actions"][0]["actor"] == "owner"
    refused = submit(lab, cid)
    assert refused["error"]["code"] == "refused" and "AWAITING_REFERENCE_APPROVAL" in refused["error"]["message"]


def test_second_campaign_for_character_refused(lab):
    cid = create(lab)
    again = api(lab, "campaign_create", project="toy", request=lab["request"], reference_id=lab["ref"])
    assert again["error"]["code"] == "conflict" and cid in again["error"]["message"]


def test_stricter_budget_needs_decision_ref_and_cannot_exceed_profile(lab):
    params = {"project": "toy", "request": lab["request"], "reference_id": lab["ref"]}
    assert api(lab, "campaign_create", **params, max_seeds_per_clip=2)["error"]["code"] == "invalid_params"
    loose = api(lab, "campaign_create", **params, max_seeds_per_clip=13, budget_decision_ref="x")
    assert loose["error"]["code"] == "refused"
    assert not (paths.state_root() / "campaigns").exists() or not list(
        (paths.state_root() / "campaigns").rglob("frozen-*")
    )


def test_torn_events_line_tolerated_and_preserved(lab):
    cid = create(lab)
    path = campaign._find(cid)
    with (path / "events.jsonl").open("ab") as stream:
        stream.write(b'{"seq":2,"type":"no')
    view = status(lab, cid)
    assert view["state"] == "READY" and view["ledger"]["torn_tail_bytes"] == 19
    ok(api(lab, "campaign_note", campaign_id=cid, text="after a torn append"))
    assert [p.read_bytes() for p in path.glob("events.torn-*")] == [b'{"seq":2,"type":"no']
    assert [e["type"] for e in events(cid)] == ["revision", "note"]
    assert status(lab, cid)["ledger"]["torn_tail_bytes"] == 0


@pytest.mark.parametrize("where", ["middle", "last", "truncate", "frozen"])
def test_tampering_is_detected_and_blocks(lab, where):
    cid = create(lab)
    for text in ("one", "two"):
        ok(api(lab, "campaign_note", campaign_id=cid, text=text))
    path = campaign._find(cid)
    log = path / "events.jsonl"
    lines = log.read_bytes().split(b"\n")[:-1]
    if where == "middle":
        lines[1] = lines[1].replace(b'"one"', b'"0ne"')
    elif where == "last":
        lines[2] = lines[2].replace(b'"two"', b'"tw0"')
    elif where == "truncate":
        lines = lines[:2]
    if where == "frozen":
        frozen = path / "frozen-r1.json"
        frozen.chmod(0o644)
        frozen.write_text(frozen.read_text().replace('"max_seeds":12', '"max_seeds":99'))
    else:
        log.write_bytes(b"".join(line + b"\n" for line in lines))
    view = status(lab, cid)
    assert view["state"] == "BLOCKED" and view["problems"][0]["code"] == "ledger_invalid"
    refused = submit(lab, cid)
    assert refused["error"]["code"] == "conflict" and "damaged" in refused["error"]["message"]


def test_busy_lock_names_holder(lab, monkeypatch):
    cid = create(lab)
    path = campaign._find(cid)
    monkeypatch.setenv("SONORANLAB_CAMPAIGN_OWNER", "orchestrator-A")
    with campaign._writer(path):
        busy = submit(lab, cid)
    assert busy["error"]["code"] == "conflict" and busy["error"]["retryable"]
    assert busy["error"]["message"].startswith("campaign_busy") and "orchestrator-A" in busy["error"]["message"]


def test_mcp_does_not_expose_owner_decisions(lab):
    names = set(registry(lab["ctx"]))
    assert {n for n in names if n.startswith("campaign_")} == {
        "campaign_create",
        "campaign_status",
        "campaign_submit",
        "campaign_decide",
        "campaign_escalate",
        "campaign_report",
        "campaign_note",
    }
    assert not [n for n in names if "owner" in n or "grant" in n]
    cid = create(lab)
    call = api(lab, "campaign_owner_decision", campaign_id=cid, kind="budget_grant")
    assert call["error"]["code"] == "invalid_params"
    for tool in ("campaign_submit", "campaign_note"):
        smuggled = api(lab, tool, campaign_id=cid, clip="idle", kind="budget_grant", seeds=5)
        assert smuggled["error"]["code"] == "invalid_params"


def test_selected_requires_technical_pass(lab, monkeypatch):
    cid = create(lab)
    job_id = ok(submit(lab, cid))["job_id"]
    early = api(lab, "campaign_decide", campaign_id=cid, job_id=job_id, decision="selected", reason="early")
    assert early["error"]["code"] == "not_ready"
    run(job_id, monkeypatch, Fake(lift={"north": 3}))
    refused = api(lab, "campaign_decide", campaign_id=cid, job_id=job_id, decision="selected", reason="looks fine")
    assert refused["error"]["code"] == "refused"


def test_report_accounting_with_nulls_overlaps_costs_and_owner_time(lab, monkeypatch, capsys):
    cid = create(lab)
    first = ok(submit(lab, cid))["job_id"]
    run(first, monkeypatch, Fake(lift={"north": 3}))
    second = ok(submit(lab, cid))["job_id"]
    run(second, monkeypatch, Fake(refuse=(1, 3)))  # guard refusal: no generation timing
    ok(api(lab, "campaign_note", campaign_id=cid, cost={"source": "orchestrator", "units": "tokens", "amount": 1200}))
    assert (
        owner(cid, "--kind", "resume", "--ref", second, "--verbatim", "ok", "--activity", "review", "--seconds", "60")
        == 0
    )
    capsys.readouterr()
    report = ok(api(lab, "campaign_report", campaign_id=cid))
    rows = report["clips"]["idle"]
    assert [r["class"] for r in rows] == ["validator_fail", "guard_refusal"]
    totals = report["totals"]
    assert totals["generation_s"]["known"] >= 1 and totals["render_s"] == {"total": None, "known": 0, "null": 2}
    assert totals["queue_wait_s"]["null"] + totals["queue_wait_s"]["known"] == 2
    assert totals["cost"] == {"orchestrator (tokens)": {"total": 1200, "known": 1, "null": 0}}
    assert totals["human_s"] == {"review": {"total": 60, "known": 1, "null": 0}}
    assert totals["admitted_need_gib"] == [40]
    assert all(r["accounting"]["memory"]["measured"] is None for r in rows)
    assert any(i["kind"] == "resume" for i in report["interventions"] if i["type"] == "owner_decision")


def test_totals_and_interval_union():
    assert campaign._total([1.5, None, 2]) == {"total": 3.5, "known": 2, "null": 1}
    assert campaign._total([None]) == {"total": None, "known": 0, "null": 1}
    t = [datetime.fromisoformat(f"2026-10-28T00:00:{s:02d}+00:00") for s in (0, 10, 5, 20, 30, 40)]
    assert campaign._union([(t[0], t[1]), (t[2], t[3]), (t[4], t[5])]) == 30.0  # 0-20 overlap + 30-40
    assert campaign._union([(t[0], t[3]), (t[2], t[1])]) == 20.0  # contained interval counted once


def test_status_envelope_is_bounded(lab, monkeypatch):
    cid = create(lab)
    run(ok(submit(lab, cid))["job_id"], monkeypatch)
    for tool in ("campaign_status", "campaign_report"):
        assert len(json.dumps(api(lab, tool, campaign_id=cid)).encode()) < 64 * 1024
