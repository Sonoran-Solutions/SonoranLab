"""Resumable class campaign ledger (DYL-129, ADR-0011): persistence and decisions over the Phase 4 job manager.

Not a scheduler or a queue. `campaign_submit` is the only generation path of a campaign; it calls the existing
durable `animate` job submission (Track E, guard budget unchanged) and records what happened.

Layout, under STATE/campaigns/<project>/<character>/<campaign_id>/:

    frozen-r<N>.json  immutable admission snapshot of revision N (exclusive create, 0444)
    events.jsonl      append-only; each line is canonical JSON whose `prev` is the sha256 of the previous line
    head.json         {seq, sha256} of the last committed line, so truncation or a rewritten last line is detected
    campaign.lock     single-writer flock (non-blocking: a second orchestrator gets `campaign_busy`)
    lock.owner        identity of the latest lock holder (informational)

State is never stored. It is derived on every read from the events, the live job store (the same reconciliation
`job_status` performs) and the registry's approval/review checks.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import socket
import stat
import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

from sonoranlab import classreq, registry
from sonoranlab.api.paths import state_root
from sonoranlab.api.registry import Context, ToolError
from sonoranlab.artifacts import sha256_file
from sonoranlab.jobs import manager, store

SCHEMA = "sonoranlab.campaign/1"
CAMPAIGN_ID = r"camp-\d{8}T\d{6}Z-[0-9a-f]{8}"
GATES = (
    "NEEDS_OWNER_INPUT",
    "NEEDS_REFERENCE_PREPARATION",
    "REFERENCES_REGISTERED",  # ADR-0011 name; registered + never approved reports AWAITING_REFERENCE_APPROVAL
    "AWAITING_REFERENCE_APPROVAL",
    "AWAITING_PROFILE_ROW",
)
PROGRESS = ("READY", "TECHNICAL_PASS", "AWAITING_OWNER_REVIEW", "ARTIFACT_APPROVED")
DELIVERY = {"staged_and_engine_verified": "STAGED_AND_ENGINE_VERIFIED", "ready_to_install": "READY_TO_INSTALL"}
DELIVERY["installed"] = "INSTALLED"
STATES = (*GATES, "READY", "GENERATING", "BLOCKED", "HOLD_BUDGET", *PROGRESS[1:], *DELIVERY.values())
OVERALL = ("BLOCKED", "GENERATING", "HOLD_BUDGET", *PROGRESS)  # the least advanced clip names the campaign
EVENTS = {
    "revision",
    "submitting",
    "submitted",
    "submit_outcome",
    "job_observed",
    "candidate_decision",
    "escalation",
    "owner_decision",
    "review_observed",
    "composed",
    "delivery",
    "note",
    "cost",
}
PRIVILEGED = {"owner_decision", "delivery"}  # written only by the owner CLI path
OWNER_KINDS = ("budget_grant", "resume", "delivery", "install", "profile_row", "reference_approval", "note")
GUARD = {"budget_refused", "guard_busy"}
INFRA = {
    "unavailable",
    "launcher_failed",
    "worker_start_failed",
    "worker_lost",
    "model_provenance",
    "inputs_changed",
    "executor_error",
}
NOT_CONSUMED = {"guard_refusal", "infra", "admission_refused"}  # never artistic budget; they still block
RESUMABLE = {"guard_refusal", "infra", "cancelled", "admission_refused", "unknown"}  # an owner `resume` clears them
STUCK = {"blocked", "foreign"}  # reconcile in the job store (cancel_job); no campaign decision clears them
IN_FLIGHT = {"active", "submitting"}
TERMINAL = {"pass", "validator_fail", "no_verdict", "generation_failed"}
LIMIT = 200
GENESIS = "0" * 64
# Test hook only (e.g. godot_check=False on a host without Godot); never caller input and never a track/memory knob.
ANIMATE_OPTIONS: dict = {}


# Set only by campaign.submit around its manager call; animate admission refuses campaign-owned clips without it.
_SUBMITTING: ContextVar[str | None] = ContextVar("campaign_submission", default=None)


def owner(project: str, character: str, anim: str) -> str | None:
    """The campaign whose latest frozen clip plan includes this clip. Read-only; an unreadable record fails closed."""
    root = state_root() / "campaigns" / project / character
    for path in sorted(root.glob("camp-*")) if root.is_dir() else ():
        frozen = sorted(path.glob("frozen-r*.json"), key=lambda f: int(f.stem.removeprefix("frozen-r")))
        try:
            if not frozen or anim in json.loads(frozen[-1].read_bytes())["clips"]:
                return path.name
        except (OSError, ValueError, KeyError, TypeError):
            return path.name
    return None


def check_ownership(params: dict) -> None:
    """Called by the animate executor's admission (under the manager's submit lock): a clip owned by a campaign is
    generated only through campaign_submit, so plain animate or retry_job cannot spend seeds the ledger can't see."""
    cid = owner(params["project"], params["character"], params["anim"])
    if cid and not (_SUBMITTING.get() or "").startswith(f"campaign/{cid}/"):
        raise ToolError(
            "refused",
            f"{params['character']}/{params['anim']} belongs to campaign {cid}; generate it through campaign_submit",
            field="anim",
            hint="campaign_submit (retry_job and animate are refused for campaign-owned clips)",
        )


class LedgerError(Exception):
    """A damaged or tampered ledger. It is preserved for inspection, never repaired or reset."""


# ---------------------------------------------------------------- storage


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(value, field: str, *, limit: int = 4096, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    errors: list[str] = []
    text = classreq._text(value, field, errors, limit=limit)
    if errors or (required and not (text or "").strip()):
        raise ToolError("invalid_params", errors[0] if errors else f"{field}: required", field=field)
    return text


def _find(campaign_id: str) -> Path:
    if not isinstance(campaign_id, str) or not re.fullmatch(CAMPAIGN_ID, campaign_id):
        raise ToolError("invalid_params", "Invalid campaign ID", field="campaign_id")
    matches = sorted((state_root() / "campaigns").glob(f"*/*/{campaign_id}"))
    if not matches:
        raise ToolError("not_found", "Unknown campaign", field="campaign_id")
    return store.directory(matches[0].relative_to(state_root()).as_posix())


def _read_bytes(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return b""
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise LedgerError(f"{path.name} is not a regular file")
        return stream.read()


def read_events(path: Path) -> tuple[list[dict], dict]:
    """(events, info). A torn last line (no newline: a crash mid-append) is ignored and reported; any other damage,
    a broken hash chain or a head that disagrees with the lines raises LedgerError."""
    data = _read_bytes(path / "events.jsonl")
    complete = data.rfind(b"\n") + 1
    events, hashes, prev = [], [], GENESIS
    for number, line in enumerate(data[:complete].split(b"\n")[:-1], 1):
        try:
            event = json.loads(line)
        except ValueError:
            raise LedgerError(f"events.jsonl line {number}: not JSON") from None
        if (
            not isinstance(event, dict)
            or event.get("seq") != number
            or event.get("prev") != prev
            or event.get("type") not in EVENTS
            or not isinstance(event.get("data"), dict)
            or store.canonical(event) != line
        ):
            raise LedgerError(f"events.jsonl line {number}: hash chain or record mismatch")
        prev = _sha(line)
        events.append(event)
        hashes.append(prev)
    try:
        head = store.load(path / "head.json") if (path / "head.json").exists() else None
    except (OSError, ValueError) as exc:
        raise LedgerError(f"head.json unreadable ({exc})") from None
    if head is None:
        if len(events) > 1:
            raise LedgerError("head.json missing")
    elif not (
        isinstance(head, dict)
        and type(head.get("seq")) is int
        and len(events) - 1 <= head["seq"] <= len(events)  # a crash may separate the append from the head update
        and (hashes[head["seq"] - 1] if head["seq"] else GENESIS) == head.get("sha256")
    ):
        raise LedgerError("head.json disagrees with events.jsonl (truncated or rewritten ledger)")
    committed = (head or {}).get("privileged", [])
    if not (isinstance(committed, list) and all(type(seq) is int for seq in committed)):
        raise LedgerError("head.json privileged list is invalid")
    ignored = [e["seq"] for e in events if e["type"] in PRIVILEGED and e["seq"] not in committed]
    info = {"events": len(events), "head": prev, "torn_tail_bytes": len(data) - complete}
    return events, info | {"privileged": committed, "ignored_privileged": ignored}


def _append(path: Path, kind: str, data: dict) -> dict:
    """Caller holds the writer lock. A torn tail is moved aside (preserved), never completed or reinterpreted."""
    events, info = read_events(path)
    fd = os.open(path / "events.jsonl", os.O_CREAT | os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as stream:
        if torn := info["torn_tail_bytes"]:
            size = os.fstat(fd).st_size
            stream.seek(size - torn)
            fragment = stream.read()
            keep = os.open(
                path / f"events.torn-{len(events) + 1}-{secrets.token_hex(4)}", os.O_CREAT | os.O_EXCL | os.O_WRONLY
            )
            with os.fdopen(keep, "wb") as out:
                out.write(fragment)
                os.fsync(out.fileno())
            os.ftruncate(fd, size - torn)
        event = {"seq": len(events) + 1, "at": _now(), "type": kind, "data": data, "prev": info["head"]}
        line = store.canonical(event)
        stream.write(line + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    store.fsync_dir(path)
    # Privileged (owner) events count only when this same writer commits their seq in head.json.
    privileged = info["privileged"] + ([event["seq"]] if kind in PRIVILEGED else [])
    store.atomic(path / "head.json", {"seq": event["seq"], "sha256": _sha(line), "privileged": privileged})
    return event


def _holder(path: Path) -> str:
    try:
        owner = store.load(path / "lock.owner")
        return f"{owner['owner']} since {owner['acquired_at']}"
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown holder"


@contextmanager
def _writer(path: Path):
    """The campaign mutation lock. Never waits: a concurrent orchestrator is told who holds it."""
    with ExitStack() as stack:
        try:
            stack.enter_context(store.lock(path / "campaign.lock", nonblocking=True))
        except BlockingIOError:
            raise ToolError(
                "conflict",
                f"campaign_busy: another orchestrator holds this campaign ({_holder(path)})",
                field="campaign_id",
                retryable=True,
                hint="wait and read campaign_status; never bypass the lock",
            ) from None
        owner = os.environ.get("SONORANLAB_CAMPAIGN_OWNER") or f"pid {os.getpid()}@{socket.gethostname()}"
        store.atomic(path / "lock.owner", {"owner": owner, "pid": os.getpid(), "acquired_at": _now()})
        yield


def _load(path: Path) -> tuple[list[dict], list[dict], dict]:
    """(events, frozen revisions in order, ledger info); frozen files must match their recorded hashes."""
    events, info = read_events(path)
    events = [e for e in events if e["seq"] not in info["ignored_privileged"]]
    revisions = []
    for event in events:
        if event["type"] != "revision":
            continue
        name = event["data"].get("frozen")
        if not isinstance(name, str) or not re.fullmatch(r"frozen-r\d+\.json", name):
            raise LedgerError(f"revision event {event['seq']}: invalid frozen name")
        data = _read_bytes(path / name)
        if not data or _sha(data) != event["data"].get("frozen_sha256"):
            raise LedgerError(f"{name}: missing or changed since revision {event['data'].get('revision')}")
        frozen = json.loads(data)
        if frozen.get("revision") != len(revisions) + 1 or frozen.get("schema") != SCHEMA:
            raise LedgerError(f"{name}: unexpected revision record")
        revisions.append(frozen)
    if not revisions:
        raise LedgerError("ledger has no revision")
    return events, revisions, info


# ---------------------------------------------------------------- admission


def _reference_gate(project: str, character: str, ref_set_id: str, frozen: dict | None, ctx: Context):
    """(blocking state or None, detail, verified hashes). Approval is recomputed from current hashes every time."""
    base = registry._base(project, character, ctx)
    if not (base / "character.json").exists():
        return "NEEDS_REFERENCE_PREPARATION", "character has no registered reference set", None
    record = registry._record(base, project, character)
    refs = next((r for r in record["reference_sets"] if r["ref_set_id"] == ref_set_id), None)
    if refs is None:
        return "NEEDS_REFERENCE_PREPARATION", f"{ref_set_id} is not registered for {character}", None
    verified = registry._verify_refs(base, refs, ctx)
    hashes, approval = verified["verification"]["hashes"], verified["approval"]
    if approval["state"] != "approved":
        detail = f"{ref_set_id} approval is {approval['state']}: " + "; ".join(approval["problems"][:5])
        return "AWAITING_REFERENCE_APPROVAL", detail.rstrip(": "), hashes
    if frozen is not None and hashes != frozen:
        return "AWAITING_REFERENCE_APPROVAL", "approved hashes differ from the frozen reference set", hashes
    return None, f"{ref_set_id} approved", hashes


def _admission(project: str, request: dict, reference_id: str, ctx: Context):
    """(state, findings, check or None, approved hashes or None) for a request + registered reference set."""
    from sonoranlab.api.tools.classreq import check_class_request  # tool modules import this one

    try:
        check = check_class_request(project, request, reference_id, ctx=ctx)
    except ToolError as exc:
        if exc.error["code"] != "not_found":
            raise
        finding = {"code": "reference_set_missing", "blocks": "needs_reference_preparation"}
        return "NEEDS_REFERENCE_PREPARATION", [finding | {"detail": exc.error["message"]}], None, None
    findings = check["findings"]
    if check["status"] == "invalid":
        return "NEEDS_OWNER_INPUT", findings, check, None
    if check["status"] == "needs_reference_preparation":
        return "NEEDS_REFERENCE_PREPARATION", findings, check, None
    state, detail, hashes = _reference_gate(project, request["character"], reference_id, None, ctx)
    if state:
        return state, [*findings, {"code": "reference_gate", "blocks": state.lower(), "detail": detail}], check, None
    if check["status"] == "needs_profile_row":
        return "AWAITING_PROFILE_ROW", findings, check, None
    return "READY", findings, check, hashes


def _budget(profile, clips: list[str], max_seeds: int | None, decision_ref: str | None) -> dict:
    autonomy = profile.autonomy
    default = autonomy.max_seeds_per_attempt * autonomy.max_attempts_per_animation
    if max_seeds is not None and max_seeds > default:
        raise ToolError(
            "refused", f"Only a stricter budget than the profile's {default} seeds", field="max_seeds_per_clip"
        )
    entry = {
        "max_seeds": default if max_seeds is None else max_seeds,
        "source": "profile_autonomy" if max_seeds is None else "owner",
        "decision_ref": decision_ref,
        "autonomy": {
            "max_seeds_per_attempt": autonomy.max_seeds_per_attempt,
            "max_attempts_per_animation": autonomy.max_attempts_per_animation,
        },
    }
    return {clip: dict(entry) for clip in clips}


def create(
    project: str,
    request: dict,
    reference_id: str,
    *,
    revise: str | None = None,
    max_seeds_per_clip: int | None = None,
    budget_decision_ref: str | None = None,
    ctx: Context,
) -> dict:
    """Admit and freeze a campaign (or revision N+1 of `revise`). Nothing is written unless admission is READY."""
    if max_seeds_per_clip is not None and budget_decision_ref is None:
        raise ToolError("invalid_params", "A stricter budget needs its owner decision reference", field="budget")
    if budget_decision_ref is not None:
        _text(budget_decision_ref, "budget_decision_ref", limit=256)
    profile = registry._profile(project, ctx)
    state, findings, check, hashes = _admission(project, request, reference_id, ctx)
    sha = classreq.request_sha256(request)
    if state != "READY":
        return {
            "created": False,
            "campaign_id": revise,
            "state": state,
            "request_sha256": sha,
            "findings": findings[:LIMIT],
            "findings_count": len(findings),
        }
    character = request["character"]
    body = {
        "schema": SCHEMA,
        "project": project,
        "character": character,
        "request": request,
        "request_sha256": sha,
        "profile": {
            "id": project,
            "sha256": sha256_file(profile.path),
            "character_row": registry._spec(profile, character),
        },
        "references": {"ref_set_id": reference_id, "hashes": hashes},
        "clips": list(request["clips"]),
        "track": "E",
        "budget": _budget(profile, list(request["clips"]), max_seeds_per_clip, budget_decision_ref),
        "admission": {"status": check["status"], "findings": findings[:LIMIT]},
    }
    base = store.directory(f"campaigns/{project}/{character}", create=True)
    with ExitStack() as stack:
        if revise is None:
            stack.enter_context(store.lock(base / ".create.lock"))
            existing = sorted(p.name for p in base.iterdir() if re.fullmatch(CAMPAIGN_ID, p.name))
            if existing:
                raise ToolError(
                    "conflict",
                    f"{character} already has campaign {existing[-1]}; a changed input is a new revision",
                    field="revise",
                    hint="call campaign_create with revise=<campaign_id>",
                )
            campaign_id = f"camp-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
            path = store.directory(f"campaigns/{project}/{character}/{campaign_id}", create=True)
            stack.enter_context(_writer(path))
            previous = None
        else:
            path = _find(revise)
            if path.parent != base:
                raise ToolError("conflict", "A revision keeps the campaign's project and character", field="revise")
            campaign_id = revise
            stack.enter_context(_writer(path))
            try:
                _, revisions, _ = _load(path)
            except LedgerError as exc:
                raise ToolError("conflict", f"Campaign ledger is damaged: {exc}") from None
            previous = revisions[-1]
            # A revision never raises a clip's cap: it carries the last frozen limit (more only via budget_grant).
            for clip in body["budget"]:
                last = next((r["budget"][clip] for r in reversed(revisions) if clip in r["budget"]), None)
                if last is None:
                    continue
                if max_seeds_per_clip is None:
                    body["budget"][clip] = last
                elif max_seeds_per_clip > last["max_seeds"]:
                    raise ToolError(
                        "refused",
                        f"{clip}: a revision cannot raise the {last['max_seeds']}-seed cap; the owner grants more",
                        field="max_seeds_per_clip",
                    )
            if {k: previous[k] for k in body if k != "admission"} == {k: body[k] for k in body if k != "admission"}:
                return {"created": False, "campaign_id": campaign_id, "state": "READY", "revision": len(revisions)}
        revision = 1 if previous is None else previous["revision"] + 1
        frozen = {
            **body,
            "campaign_id": campaign_id,
            "revision": revision,
            "previous_revision": None if previous is None else previous["revision"],
            "created_at": _now(),
        }
        name = f"frozen-r{revision}.json"
        store.atomic(path / name, frozen, exclusive=True)
        (path / name).chmod(0o444)
        data = {"revision": revision, "frozen": name, "frozen_sha256": sha256_file(path / name)}
        _append(path, "revision", data | {"previous_revision": frozen["previous_revision"]})
    return {
        "created": True,
        "campaign_id": campaign_id,
        "revision": revision,
        "state": "READY",
        "frozen_sha256": data["frozen_sha256"],
        "budget": {clip: entry["max_seeds"] for clip, entry in body["budget"].items()},
    }


# ---------------------------------------------------------------- derivation


def _key(campaign_id: str, revision: int, clip: str, seed: int) -> str:
    return f"campaign/{campaign_id}/r{revision}/{clip}/seed-{seed}"


def _lookup(key: str) -> str | None:
    """The job the existing manager created for an idempotency key: its claim, else a job record carrying the key
    hash (a crash between record and claim). Read-only; the manager is never asked to replay."""
    digest = _sha(key.encode())
    claim = state_root() / "idempotency" / f"{digest}.json"
    try:
        if claim.is_file() and not claim.is_symlink():
            job_id = store.load(claim)["job_id"]
            if re.fullmatch(store.JOB_ID, job_id):
                return job_id
    except (OSError, ValueError, KeyError, TypeError):
        pass
    root = state_root() / "jobs"
    for path in sorted(root.glob("job-*")) if root.is_dir() else ():
        try:
            if store.load(path / "job.json").get("idempotency_key_sha256") == digest:
                return path.name
        except (OSError, ValueError, AttributeError):
            continue
    return None


def _attempts(events: list[dict]) -> dict[str, dict]:
    attempts: dict[str, dict] = {}
    for event in events:
        data = event["data"]
        if event["type"] == "submitting":
            attempts[data["idempotency_key"]] = {
                "key": data["idempotency_key"],
                "clip": data["clip"],
                "seed": data["seed"],
                "revision": data["revision"],
                "job_id": None,
                "adopted": False,
                "outcome": None,
                "intent_at": event["at"],
            }
        elif event["type"] == "submitted" and data.get("idempotency_key") in attempts:
            attempts[data["idempotency_key"]].update(job_id=data["job_id"], adopted=bool(data.get("adopted")))
        elif event["type"] == "submit_outcome" and data.get("idempotency_key") in attempts:
            attempts[data["idempotency_key"]]["outcome"] = {"class": data["class"], "detail": data.get("detail")}
    return attempts


def _mismatch(job: dict, attempt: dict, frozen: dict) -> str | None:
    """Why a job record is not exactly the attempt's request (frozen revision inputs), else None."""
    params, hashes = job.get("params") or {}, job.get("input_hashes") or {}
    expected = {
        "kind": ("animate", job.get("kind")),
        "track": ("E", job.get("track")),
        "project": (frozen["project"], job.get("project")),
        "character": (frozen["character"], job.get("character")),
        "clip": (attempt["clip"], job.get("anim")),
        "seeds": ([attempt["seed"]], params.get("seeds")),
        "ref_set_id": (frozen["references"]["ref_set_id"], params.get("ref_set_id")),
        "still hashes": (frozen["references"]["hashes"], hashes.get("stills")),
        "profile hash": (frozen["profile"]["sha256"], hashes.get("profile")),
    }
    wrong = [name for name, (want, got) in expected.items() if want != got]
    return f"job {job.get('job_id')} differs from the intent: {', '.join(wrong)}" if wrong else None


def _classify(attempt: dict, frozen: dict, ctx: Context) -> dict:
    """Attach the live job and its outcome class. Status of a recorded job uses job_status (same reconciliation)."""
    a = dict(attempt)
    if a["job_id"] is None and a["outcome"] is None:
        found = _lookup(a["key"])
        if found is None:
            return a | {"class": "submitting", "job": None}  # resolved to adopt/unknown by the next mutating call
        a["job_id"], a["adoptable"] = found, True
    if a["job_id"] is None:
        return a | {"class": a["outcome"]["class"], "job": None}
    from sonoranlab.api.tools.jobs import job_status

    try:
        status = job_status({"job_id": a["job_id"]}, ctx)
    except (ToolError, OSError, ValueError) as exc:
        return a | {"class": "unknown", "job": None, "detail": f"job record unreadable or missing ({exc})"}
    error = status.get("error") or {}
    job = {
        "state": status["state"],
        "stage": status.get("stage"),
        "error": {k: error.get(k) for k in ("code", "message")} if error else None,
        "technical": status.get("technical"),
        "human": {k: (status.get("human") or {}).get(k) for k in ("review", "production_usable")},
        "prompts": len(status.get("prompts") or []),
    }
    summary = status.get("result_summary") or {}
    verdict = (summary.get("animate") or {}).get("verdict") or {}
    a["verdict"] = {
        "pass": verdict.get("pass"),
        "failures": verdict.get("failures", [])[:20],
        "failures_count": verdict.get("failures_count", len(verdict.get("failures", []))),
        "warnings_count": verdict.get("warnings_count", len(verdict.get("warnings", []))),
    }
    a["status"] = status  # internal: report accounting; stripped from outputs
    mismatch = None if manager.corrupt(status) else _mismatch(status, a, frozen)
    if mismatch and a.get("adoptable"):
        return a | {"class": "unknown", "job": None, "detail": "adoption refused: " + mismatch}
    if manager.corrupt(status):
        cls = "blocked"
    elif mismatch:
        a["detail"] = mismatch
        cls = "foreign"
    elif status["state"] in store.ACTIVE:
        cls = "active"
    elif status["state"] == "blocked":
        cls = "blocked"
    elif status["state"] == "succeeded":
        validator = (status.get("technical") or {}).get("validator")
        cls = "pass" if validator == "pass" else "validator_fail" if validator == "fail" else "no_verdict"
    elif status["state"] == "cancelled":
        cls = "cancelled"
    else:
        code = error.get("code")
        cls = "guard_refusal" if code in GUARD else "infra" if code in INFRA else "generation_failed"
    return a | {"class": cls, "job": job}


def _public(attempt: dict) -> dict:
    keys = (
        "key",
        "clip",
        "seed",
        "revision",
        "current",
        "job_id",
        "adopted",
        "class",
        "consumed",
        "blocking",
        "resumed",
    )
    out = {k: attempt.get(k) for k in keys}
    out["job"] = attempt.get("job")
    if attempt.get("verdict"):
        out["verdict"] = attempt["verdict"]
    if attempt.get("outcome"):
        out["outcome"] = attempt["outcome"]
    if attempt.get("detail"):
        out["detail"] = attempt["detail"]
    return out


def _attempt_usage(project: str, character: str, clip: str, jobs: list[dict]) -> list[str]:
    """The manager's animate admission rule (jobs/kinds/generation.py `admit`), read-only."""
    return [
        j["job_id"]
        for j in jobs
        if j.get("kind") == "animate"
        and (j.get("project"), j.get("character"), j.get("anim")) == (project, character, clip)
        and (j["state"] in store.ACTIVE or j.get("prompts"))
    ]


def _binding(frozen: dict) -> tuple:
    return frozen["references"]["hashes"], frozen["profile"]["character_row"], frozen["request_sha256"]


def _composed_approvals(events: list[dict], current: set, campaign_id: str, ctx: Context) -> dict[str, dict]:
    """{composed job id: {clip: input job}} for recorded current-revision compose_sheet jobs of this campaign whose
    owner review is production usable now (recomputed via the import's review) and whose sheet is unchanged."""
    out = {}
    for event in events:
        data = event["data"]
        if event["type"] != "composed" or data.get("revision") not in current:
            continue
        try:
            job = manager.get(data["job_id"])
            result = job.get("result") or {}
            if (
                manager.corrupt(job)
                or (job["kind"], job["state"]) != ("compose_sheet", "succeeded")
                or job["params"].get("campaign_id") != campaign_id
                or result.get("artifact_id") != data["artifact_id"]
                or sha256_file(Path(result["export"])) != data["sheet_sha256"]
                or not registry.check_review(data["artifact_id"], ctx=ctx)["production_usable"]
            ):
                continue
            out[data["job_id"]] = {c["name"]: c["job"] for c in job["params"]["clips"]}
        except (ToolError, OSError, ValueError, KeyError, TypeError):
            continue
    return out


def _bundle_state(campaign_id: str, composed: set, jobs: list[dict], ctx: Context) -> str | None:
    """Most advanced verified state of an intact bundle built from an approved composed job (delivery_status logic)."""
    from sonoranlab.api.tools.delivery import delivery_status  # the delivery tools import this module

    best = None
    for job in jobs:
        params = job.get("params") or {}
        if not (
            job.get("kind") == "build_delivery"
            and job["state"] == "succeeded"
            and params.get("campaign_id") == campaign_id
            and params.get("composed") in composed
        ):
            continue
        for bundle in delivery_status(bundle=job["job_id"], ctx=ctx)["bundles"]:
            states = bundle.get("states") or {}  # INVALID bundles carry none
            state = next((s for s in ("READY_TO_INSTALL", "STAGED_AND_ENGINE_VERIFIED") if states.get(s)), None)
            if state and (best is None or state == "READY_TO_INSTALL"):
                best = state
    return best


def check_composition(campaign_id: str, params: dict, ctx: Context) -> int:
    """compose_sheet with campaign_id: every input clip job must be the campaign's current-revision selected
    candidate for that clip. Returns the current revision."""
    view = _view(_find(campaign_id), ctx)
    if "_events" not in view:
        raise ToolError("conflict", "Campaign ledger is damaged", field="campaign_id")
    frozen = view["_revisions"][-1]
    if (frozen["project"], frozen["character"]) != (params["project"], params["character"]):
        raise ToolError("refused", "Campaign names a different project/character", field="campaign_id")
    if params["reference_id"] != frozen["references"]["ref_set_id"]:
        raise ToolError("refused", "reference_id is not the campaign's frozen reference set", field="reference_id")
    if view["gate"]["state"] != "READY":
        raise ToolError("refused", f"{view['gate']['state']}: {view['gate']['detail']}", field="campaign_id")
    for clip in params["clips"]:
        info = view["clips"].get(clip["name"])
        if not info or not info["in_plan"] or info["selected"] != clip["job"]:
            raise ToolError(
                "refused",
                f"{clip['job']} is not the current-revision selected candidate for {clip['name']}",
                field="clips",
                hint="campaign_decide selected on a current-revision passing job first",
            )
    return frozen["revision"]


def record_composed(campaign_id: str, params: dict, job_id: str, artifact_id: str, sheet_sha256: str, ctx: Context):
    """Append the `composed` event under the writer lock (waits briefly if another writer holds it)."""
    path = _find(campaign_id)
    for _ in range(120):
        try:
            with _writer(path):
                revision = check_composition(campaign_id, params, ctx)
                data = {"job_id": job_id, "artifact_id": artifact_id, "sheet_sha256": sheet_sha256}
                inputs = {c["name"]: c["job"] for c in params["clips"]}
                return _append(path, "composed", data | {"inputs": inputs, "revision": revision})
        except ToolError as exc:
            if not exc.error["message"].startswith("campaign_busy"):
                raise
            time.sleep(0.5)
    raise ToolError("conflict", "campaign_busy: could not record the composed sheet", field="campaign_id")


def _gate(frozen: dict, ctx: Context) -> tuple[str, str]:
    project, character = frozen["project"], frozen["character"]
    refs = frozen["references"]
    state, detail, _ = _reference_gate(project, character, refs["ref_set_id"], refs["hashes"], ctx)
    if state:
        return state, detail
    profile = registry._profile(project, ctx)
    if character not in profile.characters:
        return "AWAITING_PROFILE_ROW", f"profile {project} has no characters.{character} row"
    if sha256_file(profile.path) != frozen["profile"]["sha256"] or (
        registry._spec(profile, character) != frozen["profile"]["character_row"]
    ):
        return "NEEDS_OWNER_INPUT", f"profile changed since revision {frozen['revision']}: revision_required"
    return "READY", "references approved and unchanged; profile row frozen"


def _view(path: Path, ctx: Context) -> dict:
    """Derived campaign state. Never written; recomputed from events, the job store and registry checks."""
    try:
        events, revisions, info = _load(path)
    except LedgerError as exc:
        return {
            "campaign_id": path.name,
            "state": "BLOCKED",
            "problems": [{"code": "ledger_invalid", "detail": str(exc)}],
            "next_actions": [
                {"actor": "owner", "action": "inspect", "why": "ledger damaged; preserve it, nothing may write"}
            ],
        }
    frozen = revisions[-1]
    project, character = frozen["project"], frozen["character"]
    owner = [e["data"] for e in events if e["type"] == "owner_decision"]
    resumed = {d.get("ref") for d in owner if d.get("kind") == "resume"}
    decisions: dict[str, dict] = {}
    for event in events:
        if event["type"] == "candidate_decision":
            decisions[event["data"]["job_id"]] = event["data"] | {"at": event["at"]}
    # Candidates, selections, reviews and delivery count only when bound to the current frozen inputs.
    current = {r["revision"] for r in revisions if _binding(r) == _binding(frozen)}
    attempts = []
    for attempt in _attempts(events).values():
        a = _classify(attempt, revisions[attempt["revision"] - 1], ctx)
        a["current"] = a["revision"] in current
        a["resumed"] = a["class"] in RESUMABLE and bool({a["key"], a["job_id"]} & resumed)
        prompts = (a.get("job") or {}).get("prompts", 0)
        a["consumed"] = a["class"] not in NOT_CONSUMED and not (a["class"] == "cancelled" and not prompts)
        a["blocking"] = (a["class"] in RESUMABLE and not a["resumed"]) or a["class"] in STUCK
        attempts.append(a)
    jobs = manager.reconcile_all()
    gate, gate_detail = _gate(frozen, ctx)
    clips = {}
    plan = frozen["clips"]
    composed = _composed_approvals(events, current, frozen["campaign_id"], ctx)
    for clip in [*plan, *sorted({a["clip"] for a in attempts} - set(plan))]:
        mine = [a for a in attempts if a["clip"] == clip]
        base = frozen["budget"].get(clip, {}).get("max_seeds", 0)
        granted = sum(d.get("seeds", 0) for d in owner if d.get("kind") == "budget_grant" and d.get("clip") == clip)
        consumed = sum(a["consumed"] for a in mine)
        used = _attempt_usage(project, character, clip, jobs)
        limit = registry._profile(project, ctx).autonomy.max_attempts_per_animation
        candidates = [
            {
                "job_id": a["job_id"],
                "seed": a["seed"],
                "revision": a["revision"],
                "technical": a["job"]["technical"],
                "human": a["job"]["human"],
                "verdict": a.get("verdict"),
                "decision": decisions.get(a["job_id"], {}).get("decision"),
            }
            for a in mine
            if a["class"] == "pass" and a["current"]
        ]
        live = [c for c in candidates if c["decision"] != "rejected"]
        selected = next((c for c in reversed(live) if c["decision"] == "selected"), None)
        remaining = base + granted - consumed
        if any(a["blocking"] for a in mine):
            state, reason = "BLOCKED", "unresolved or non-artistic outcome; report and wait"
        elif any(a["class"] in IN_FLIGHT for a in mine):
            state, reason = "GENERATING", "a job is in flight"
        elif selected and selected["human"]["production_usable"]:
            state, reason = "ARTIFACT_APPROVED", f"owner review of {selected['job_id']} is production usable"
        elif selected and (sheet := next((j for j, ins in composed.items() if ins.get(clip) == selected["job_id"]), 0)):
            state, reason = (
                "ARTIFACT_APPROVED",
                f"owner review of composed sheet {sheet} (contains {selected['job_id']})",
            )
        elif selected:
            state, reason = "AWAITING_OWNER_REVIEW", f"{selected['job_id']} recommended; owner 1x review pending"
        elif live:
            state, reason = "TECHNICAL_PASS", "technically passing candidate(s) await the agent's decision"
        elif remaining <= 0:
            state, reason = "HOLD_BUDGET", f"seed budget spent ({consumed}/{base + granted})"
        elif len(used) >= limit:
            state, reason = "HOLD_BUDGET", f"profile max_attempts_per_animation reached ({len(used)}/{limit})"
        else:
            state, reason = "READY", "budget remains"
        clips[clip] = {
            "in_plan": clip in plan,
            "state": state,
            "reason": reason,
            "budget": {
                "base": base,
                "granted": granted,
                "consumed": consumed,
                "remaining": remaining,
                "not_consumed": sum(not a["consumed"] for a in mine),
            },
            "attempt_limit": {"used": len(used), "limit": limit, "jobs": used[:LIMIT]},
            "seeds_used": sorted({a["seed"] for a in mine}),
            "candidates": candidates[:LIMIT],
            "selected": selected["job_id"] if selected else None,
            "attempts": [_public(a) for a in mine][:LIMIT],
            "attempts_truncated": len(mine) > LIMIT,
        }
    planned = [clips[c]["state"] for c in plan]
    if gate != "READY":
        state = gate
    elif any(a["blocking"] for a in attempts):
        state = "BLOCKED"
    elif any(a["class"] in IN_FLIGHT for a in attempts):
        state = "GENERATING"
    else:
        state = next(s for s in OVERALL if s in planned)
        delivered = [
            e["data"]["state"] for e in events if e["type"] == "delivery" and e["data"].get("revision") in current
        ]
        if state == "ARTIFACT_APPROVED":
            # Bundles give STAGED/READY only; INSTALLED comes only from an owner CLI record.
            found = [_bundle_state(frozen["campaign_id"], set(composed), jobs, ctx)]
            found += [DELIVERY[d] for d in delivered[-1:]]
            order = list(DELIVERY.values())
            state = max((f for f in found if f), key=order.index, default=state)
    view = {
        "schema": "sonoranlab.campaign_status/1",
        "campaign_id": frozen["campaign_id"],
        "project": project,
        "character": character,
        "revision": frozen["revision"],
        "track": frozen["track"],
        "state": state,
        "gate": {"state": gate, "detail": gate_detail},
        "clips": clips,
        "problems": [
            {
                "code": "uncommitted_privileged_event",
                "seq": seq,
                "detail": "owner event not committed by the owner CLI; ignored",
            }
            for seq in info["ignored_privileged"]
        ],
        "ledger": info,
        "_attempts": attempts,
        "_events": events,
        "_revisions": revisions,
    }
    view["next_actions"] = _next_actions(view)
    return view


def _next_actions(view: dict) -> list[dict]:
    gate = view["gate"]["state"]
    owner = {
        "NEEDS_REFERENCE_PREPARATION": "prepare and register the eight stills (owner refprep, register_character)",
        "AWAITING_REFERENCE_APPROVAL": "owner runs `sonoranlab registry approve-references` for the frozen set, or "
        "the agent revises the campaign to an approved set",
        "AWAITING_PROFILE_ROW": "owner reviews and merges the characters row",
    }
    if gate == "NEEDS_OWNER_INPUT":
        return [{"actor": "agent", "action": "campaign_create", "why": view["gate"]["detail"] + " (pass revise)"}]
    if gate != "READY":
        return [{"actor": "owner", "action": "gate", "why": owner[gate]}]
    actions = []
    for a in view["_attempts"]:
        if a["class"] in STUCK:
            actions.append(
                {
                    "actor": "agent",
                    "action": "report",
                    "job_id": a["job_id"],
                    "why": "job outcome unresolved; reconcile with job_status/cancel_job, never resubmit",
                }
            )
        elif a["blocking"]:
            actions.append(
                {
                    "actor": "owner",
                    "action": "owner-decision resume",
                    "ref": a["job_id"] or a["key"],
                    "why": f"{a['class']} is not an artistic failure and never authorizes a blind retry",
                }
            )
    if any(a["class"] in IN_FLIGHT for a in view["_attempts"]):
        actions.append({"actor": "agent", "action": "campaign_status", "why": "poll; a job is in flight"})
    if actions:
        return actions
    for clip, c in view["clips"].items():
        if not c["in_plan"]:
            continue
        if c["state"] == "READY":
            actions.append({"actor": "agent", "action": "campaign_submit", "clip": clip, "why": c["reason"]})
        elif c["state"] == "TECHNICAL_PASS":
            actions.append({"actor": "agent", "action": "campaign_decide", "clip": clip, "why": c["reason"]})
            if c["budget"]["remaining"] > 0:
                actions.append({"actor": "agent", "action": "campaign_submit", "clip": clip, "why": "optional"})
        elif c["state"] == "HOLD_BUDGET":
            actions.append({"actor": "agent", "action": "campaign_escalate", "clip": clip, "why": c["reason"]})
            actions.append({"actor": "owner", "action": "owner-decision budget_grant", "clip": clip, "why": "adds"})
        elif c["state"] == "AWAITING_OWNER_REVIEW":
            actions.append(
                {"actor": "owner", "action": "sonoranlab review record", "job_id": c["selected"], "why": c["reason"]}
            )
    if view["state"] in {"ARTIFACT_APPROVED", "STAGED_AND_ENGINE_VERIFIED", "READY_TO_INSTALL"}:
        actions.append({"actor": "owner", "action": "delivery", "why": "staging and installation are separate steps"})
    return actions


def _strip(view: dict) -> dict:
    return {k: v for k, v in view.items() if not k.startswith("_")}


def _observe(path: Path, view: dict) -> None:
    """Under the writer lock: record intent resolutions, newly observed job outcomes and review changes."""
    events = view.get("_events", [])
    seen: dict[str, tuple] = {}
    reviews: dict[str, bool] = {}
    for event in events:
        if event["type"] == "job_observed":
            seen[event["data"]["job_id"]] = (event["data"]["state"], event["data"]["class"])
        elif event["type"] == "review_observed":
            reviews[event["data"]["job_id"]] = event["data"]["production_usable"]
    for a in view.get("_attempts", []):
        if a["job_id"] is None:
            continue
        job = a["job"]
        if job and (job["state"], a["class"]) != seen.get(a["job_id"]) and a["class"] not in IN_FLIGHT:
            verdict = (job.get("technical") or {}).get("validator")
            data = {"job_id": a["job_id"], "state": job["state"], "class": a["class"], "verdict": verdict}
            _append(path, "job_observed", data | {"error": job["error"]})
        usable = job and job["human"]["production_usable"]
        if a["class"] == "pass" and usable != reviews.get(a["job_id"], False):
            data = {"job_id": a["job_id"], "artifact": a["job_id"], "production_usable": bool(usable)}
            _append(path, "review_observed", data | {"review": job["human"]["review"]})


def _reconcile(path: Path, ctx: Context) -> dict:
    """Under the writer lock. An intent without a job: adopt the job its key created, else mark it unknown (BLOCKED,
    never resubmitted). Then record observations and return the fresh view."""
    try:
        events, revisions, _ = _load(path)
    except LedgerError as exc:
        raise ToolError("conflict", f"Campaign ledger is damaged: {exc}", hint="preserve it for inspection") from None
    for a in _attempts(events).values():
        if a["job_id"] is None and a["outcome"] is None:
            base = {"idempotency_key": a["key"], "clip": a["clip"], "seed": a["seed"]}
            found = _lookup(a["key"])
            try:
                mismatch = found and _mismatch(manager.get(found), a, revisions[a["revision"] - 1])
            except (ToolError, OSError, ValueError) as exc:
                mismatch = f"job {found} unreadable ({exc})"
            if found and not mismatch:
                _append(path, "submitted", base | {"job_id": found, "adopted": True})
            else:
                detail = "intent recorded but the manager has no job for its key; not resubmitted"
                if mismatch:
                    detail = f"adoption refused: {mismatch}; not resubmitted"
                _append(path, "submit_outcome", base | {"class": "unknown", "detail": detail})
    view = _view(path, ctx)
    _observe(path, view)
    return _view(path, ctx)


# ---------------------------------------------------------------- operations


def status(campaign_id: str, *, ctx: Context) -> dict:
    return _strip(_view(_find(campaign_id), ctx))


def submit(campaign_id: str, clip: str, seed: int | None = None, *, ctx: Context) -> dict:
    path = _find(campaign_id)
    with _writer(path):
        view = _reconcile(path, ctx)
        if view["gate"]["state"] != "READY":
            raise ToolError("refused", f"{view['gate']['state']}: {view['gate']['detail']}", field="campaign_id")
        frozen = view["_revisions"][-1]
        if clip not in frozen["clips"]:
            raise ToolError("invalid_params", f"{clip!r} is not in the campaign's clip plan", field="clip")
        attempts = view["_attempts"]
        if blocked := [a["job_id"] or a["key"] for a in attempts if a["blocking"]]:
            raise ToolError(
                "refused",
                "BLOCKED: unresolved or non-artistic outcomes: " + ", ".join(blocked[:10]),
                field="campaign_id",
                hint="report and wait; reconcile stuck jobs, or the owner records a resume decision",
            )
        if flying := [a["job_id"] or a["key"] for a in attempts if a["class"] in IN_FLIGHT]:
            raise ToolError("conflict", "GENERATING: " + ", ".join(flying[:10]), field="campaign_id", retryable=True)
        c = view["clips"][clip]
        if c["state"] in {"AWAITING_OWNER_REVIEW", "ARTIFACT_APPROVED"}:
            raise ToolError("refused", f"{c['state']}: {c['reason']}", field="clip")
        if c["state"] == "HOLD_BUDGET":
            raise ToolError("refused", f"HOLD_BUDGET: {c['reason']}", field="clip", hint="campaign_escalate")
        # Numeric checks independent of state precedence (a passing candidate must not hide a spent budget).
        spent, limit = c["budget"], c["attempt_limit"]
        if spent["remaining"] <= 0 or limit["used"] >= limit["limit"]:
            total = spent["base"] + spent["granted"]
            reason = f"{spent['consumed']}/{total} seeds, {limit['used']}/{limit['limit']} attempts"
            raise ToolError("refused", f"HOLD_BUDGET: {reason}", field="clip", hint="campaign_escalate")
        used = {a["seed"] for a in attempts if a["clip"] == clip}
        if seed is None:
            seed = next(s for s in range(len(used) + 1) if s not in used)
        elif seed in used:
            raise ToolError(
                "conflict", f"Seed {seed} was already used for {clip}; seeds are never reused", field="seed"
            )
        key = _key(campaign_id, frozen["revision"], clip, seed)
        data = {"clip": clip, "seed": seed, "revision": frozen["revision"], "idempotency_key": key}
        _append(path, "submitting", data)
        params = {
            "project": frozen["project"],
            "character": frozen["character"],
            "ref_set_id": frozen["references"]["ref_set_id"],
            "anim": clip,
            "seeds": [seed],
            **ANIMATE_OPTIONS,
        }
        token = _SUBMITTING.set(key)
        try:
            job = manager.submit("animate", params, key)
        except ToolError as exc:
            outcome = {"idempotency_key": key, "clip": clip, "seed": seed, "class": "admission_refused"}
            _append(path, "submit_outcome", outcome | {"detail": exc.error})
            raise
        finally:
            _SUBMITTING.reset(token)
        _append(path, "submitted", {"idempotency_key": key, "clip": clip, "seed": seed, "job_id": job["job_id"]})
        after = _view(path, ctx)["clips"][clip]
        return {
            "campaign_id": campaign_id,
            "clip": clip,
            "seed": seed,
            "idempotency_key": key,
            "job_id": job["job_id"],
            "job_state": job["state"],
            "budget": after["budget"],
        }


def _attempt_for(view: dict, job_id: str) -> dict:
    found = next((a for a in view["_attempts"] if a["job_id"] == job_id), None)
    if found is None:
        raise ToolError("not_found", "Job is not part of this campaign", field="job_id")
    return found


def decide(campaign_id: str, job_id: str, decision: str, reason: str, *, ctx: Context) -> dict:
    """The agent's candidate recommendation; never an approval."""
    reason = _text(reason, "reason")
    if decision not in {"selected", "rejected"}:
        raise ToolError("invalid_params", "decision must be selected or rejected", field="decision")
    path = _find(campaign_id)
    with _writer(path):
        view = _reconcile(path, ctx)
        a = _attempt_for(view, job_id)
        if a["class"] in IN_FLIGHT or a["job"] is None:
            raise ToolError("not_ready", "Job has no terminal outcome yet", field="job_id", retryable=True)
        if decision == "selected" and not a["current"]:
            raise ToolError("refused", "Job belongs to inputs of an earlier revision", field="job_id")
        if decision == "selected" and a["class"] != "pass":
            raise ToolError("refused", f"Only a technically passing job can be selected ({a['class']})", field="job_id")
        _append(
            path, "candidate_decision", {"job_id": job_id, "clip": a["clip"], "decision": decision, "reason": reason}
        )
        after = _view(path, ctx)
        return {
            "campaign_id": campaign_id,
            "job_id": job_id,
            "decision": decision,
            "clip_state": after["clips"][a["clip"]]["state"],
        }


def _rank(a: dict) -> tuple:
    verdict = a.get("verdict") or {}
    return (
        a["class"] != "pass",
        verdict.get("failures_count", math.inf),
        verdict.get("warnings_count", math.inf),
        a["intent_at"],
    )


def escalate(campaign_id: str, clip: str, reason: str, *, ctx: Context) -> dict:
    """Best two candidates by the validator ranking already in job results (pass first, then fewest failures, then
    fewest warnings, then oldest) with their exact diagnostics."""
    reason = _text(reason, "reason")
    path = _find(campaign_id)
    with _writer(path):
        view = _reconcile(path, ctx)
        if clip not in view["clips"]:
            raise ToolError("invalid_params", f"{clip!r} is not a campaign clip", field="clip")
        finished = sorted((a for a in view["_attempts"] if a["clip"] == clip and a["class"] in TERMINAL), key=_rank)
        best = [
            {
                "job_id": a["job_id"],
                "seed": a["seed"],
                "class": a["class"],
                "technical": a["job"]["technical"],
                "verdict": a.get("verdict"),
                "error": a["job"]["error"],
            }
            for a in finished[:2]
        ]
        data = {"clip": clip, "reason": reason, "best_two": [b["job_id"] for b in best], "diagnostics": best}
        data["budget"] = view["clips"][clip]["budget"]
        event = _append(path, "escalation", data)
        return {"campaign_id": campaign_id, "seq": event["seq"], **data}


def note(campaign_id: str, text: str | None = None, cost: dict | None = None, *, ctx: Context) -> dict:
    """Agent notes and explicit cost records (source + units); costs are never inferred."""
    if text is None and cost is None:
        raise ToolError("invalid_params", "A note needs text or cost", field="text")
    path = _find(campaign_id)
    with _writer(path):
        try:
            _load(path)
        except LedgerError as exc:
            raise ToolError("conflict", f"Campaign ledger is damaged: {exc}") from None
        written = []
        if text is not None:
            written.append(_append(path, "note", {"text": _text(text, "text")})["seq"])
        if cost is not None:
            amount = cost.get("amount")
            if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0:
                raise ToolError("invalid_params", "cost.amount must be a finite number >= 0", field="cost")
            data = {"source": _text(cost.get("source"), "cost.source", limit=200)}
            data |= {"units": _text(cost.get("units"), "cost.units", limit=64), "amount": amount}
            written.append(_append(path, "cost", data)["seq"])
        return {"campaign_id": campaign_id, "seqs": written}


def owner_decision(
    campaign_id: str,
    kind: str,
    verbatim: str,
    *,
    ref: str | None = None,
    clip: str | None = None,
    seeds: int | None = None,
    state: str | None = None,
    activity: str | None = None,
    seconds: float | None = None,
    ctx: Context,
) -> dict:
    """Owner-only (CLI; never an MCP tool). A budget grant ADDS seeds; history is never zeroed."""
    verbatim = _text(verbatim, "verbatim")
    if kind not in OWNER_KINDS:
        raise ToolError("invalid_params", f"kind must be one of {', '.join(OWNER_KINDS)}", field="kind")
    data: dict = {"kind": kind, "verbatim": verbatim, "ref": _text(ref, "ref", limit=512, required=False)}
    if (activity is None) != (seconds is None):
        raise ToolError("invalid_params", "human time needs both activity and seconds", field="seconds")
    if activity is not None:
        if activity not in {"preparation", "cleanup", "review"} or not (math.isfinite(seconds) and seconds >= 0):
            raise ToolError("invalid_params", "activity preparation|cleanup|review and seconds >= 0", field="activity")
        data["human_time"] = {"activity": activity, "seconds": seconds}
    path = _find(campaign_id)
    with _writer(path):
        view = _reconcile(path, ctx)
        if kind == "budget_grant":
            if clip not in view["clips"] or type(seeds) is not int or not 1 <= seeds <= 1000:
                raise ToolError("invalid_params", "budget_grant needs a campaign clip and 1..1000 seeds", field="seeds")
            data |= {"clip": clip, "seeds": seeds}
        if kind == "resume":
            match = [a for a in view["_attempts"] if ref in (a["key"], a["job_id"]) and a["class"] in RESUMABLE]
            if not match or not match[0]["blocking"]:
                raise ToolError("refused", "resume names a blocked, resumable attempt (job_id or key)", field="ref")
        if kind in {"delivery", "install"} and not data["ref"]:
            raise ToolError("invalid_params", f"{kind} needs --ref naming the bundle", field="ref")
        if kind == "delivery":
            if state not in DELIVERY:
                raise ToolError("invalid_params", f"state must be one of {', '.join(DELIVERY)}", field="state")
            installs = [
                e["data"]["ref"]
                for e in view["_events"]
                if e["type"] == "owner_decision" and e["data"]["kind"] == "install"
            ]
            if state == "installed" and data["ref"] not in installs:
                raise ToolError("refused", "installed needs a prior install authorization for this bundle", field="ref")
            data |= {"state": state, "bundle": data["ref"], "revision": view["revision"]}
            event = _append(path, "delivery", data)
        else:
            event = _append(path, "owner_decision", data)
        return {"campaign_id": campaign_id, "seq": event["seq"], "type": event["type"], **event["data"]}


# ---------------------------------------------------------------- accounting


def _total(values: list) -> dict:
    known = [v for v in values if v is not None]
    return {"total": round(sum(known), 3) if known else None, "known": len(known), "null": len(values) - len(known)}


def _union(intervals: list[tuple[datetime, datetime]]) -> float:
    """Wall seconds covered by possibly overlapping intervals, each counted once."""
    total, end = 0.0, None
    for start, stop in sorted(intervals):
        if end is None or start > end:
            total += (stop - start).total_seconds()
            end = stop
        elif stop > end:
            total += (stop - end).total_seconds()
            end = stop
    return round(total, 3)


def _accounting(a: dict) -> dict:
    status = a.get("status") or {}
    timing = status.get("timing") or {}
    measured = (status.get("progress") or {}).get("timing") or {}
    started = (status.get("worker") or {}).get("started_at")
    ended = next(
        (h["at"] for h in reversed(status.get("history") or []) if h["state"] in store.TERMINAL | {"blocked"}), None
    )
    memory = None
    if a["job_id"] and status:
        try:
            result = manager.get(a["job_id"]).get("result") or {}
            samples = [x["memory"] for x in result.get("attempts", []) if x.get("memory") is not None]
            memory = samples or None
        except (ToolError, OSError, ValueError):
            memory = None
    return {
        "queue_wait_s": timing.get("queue_wait_s"),
        "generation_s": measured.get("generate"),
        "processing_s": measured.get("select_and_process"),
        "validate_export_s": measured.get("validate_export"),
        "render_s": None,  # no per-attempt render timing is measured by the animate job today
        "run_s": timing.get("run_s"),
        "interval": [started, ended] if started and ended else None,
        "memory": {"admitted_need_gib": status.get("need_gib"), "measured": memory},
    }


def report(campaign_id: str, *, ctx: Context) -> dict:
    view = _view(_find(campaign_id), ctx)
    if "_events" not in view:
        return _strip(view)
    events = view["_events"]
    clips, rows = {}, []
    for a in view["_attempts"]:
        row = {**_public(a), "accounting": _accounting(a)}
        rows.append(row)
        clips.setdefault(a["clip"], []).append(row)
    keys = ("queue_wait_s", "generation_s", "processing_s", "validate_export_s", "render_s", "run_s")
    intervals = [
        tuple(datetime.fromisoformat(x) for x in r["accounting"]["interval"])
        for r in rows
        if r["accounting"]["interval"]
    ]
    human: dict[str, list] = {}
    for event in events:
        if event["type"] == "owner_decision" and event["data"].get("human_time"):
            time = event["data"]["human_time"]
            human.setdefault(time["activity"], []).append(time["seconds"])
    costs: dict[str, list] = {}
    for event in events:
        if event["type"] == "cost":
            costs.setdefault(f"{event['data']['source']} ({event['data']['units']})", []).append(
                event["data"]["amount"]
            )
    interventions = [
        {
            "seq": e["seq"],
            "at": e["at"],
            "type": e["type"],
            **{k: v for k, v in e["data"].items() if k != "diagnostics"},
        }
        for e in events
        if e["type"] in {"owner_decision", "escalation", "candidate_decision", "delivery", "note"}
    ]
    return {
        "schema": "sonoranlab.campaign_report/1",
        "campaign_id": view["campaign_id"],
        "state": view["state"],
        "revision": view["revision"],
        "clips": {clip: items[:LIMIT] for clip, items in clips.items()},
        "attempts_truncated": any(len(items) > LIMIT for items in clips.values()),
        "totals": {
            **{key: _total([r["accounting"][key] for r in rows]) for key in keys},
            "wall_union_s": _union(intervals) if intervals else None,
            "wall_intervals": len(intervals),
            "human_s": {activity: _total(values) for activity, values in human.items()},
            "cost": {name: _total(values) for name, values in costs.items()},
            "admitted_need_gib": sorted({r["accounting"]["memory"]["admitted_need_gib"] for r in rows} - {None}),
        },
        "interventions": interventions[-LIMIT:],
        "interventions_count": len(interventions),
    }
