"""Optional acceptance handoff; editor code is imported only inside handlers."""

from __future__ import annotations

import fcntl
import json
import os
import re
from collections import Counter
from functools import partial
from pathlib import Path
from uuid import uuid4

from sonoranlab.api.registry import Context, Tool, ToolError
from sonoranlab.artifacts import sha256_file

REPAIR_ID = r"[a-z0-9][a-z0-9_-]{0,127}"


def editor_available(ctx: Context) -> bool:
    return Path(os.environ.get("SONORANLAB_EDITOR_RUNTIME", "~/sonoranlab-work/phase3.5/runtime")).expanduser().is_dir()


def invoke(operation, *args, **kwargs):
    from sonoranlab.repair import EditorUnavailable, Refused

    try:
        return operation(*args, **kwargs)
    except EditorUnavailable as exc:
        raise ToolError("unavailable", str(exc)) from exc
    except Refused as exc:
        raise ToolError("refused", str(exc)) from exc


def repair_path(identifier: str) -> Path:
    from sonoranlab.repair.confine import admit_repair, repair_root

    if not isinstance(identifier, str) or not re.fullmatch(REPAIR_ID, identifier):
        raise ToolError("invalid_params", "Expected a repair directory id", field="repair_id")
    return invoke(admit_repair, invoke(repair_root) / identifier)


def identity(repair: Path, doc: dict) -> dict:
    from sonoranlab.repair.authorization import source_revision

    return {
        "manifest_repair_id": doc["repair_id"],
        "manifest_sha256": sha256_file(repair / "repair.json"),
        "mapping_sha256": sha256_file(repair / "mapping.json"),
        "profile_sha256": doc["profile"]["sha256"],
        "parent_hashes": doc["parent_hashes"],
        "source_hashes": doc["source"],
        "source_revision": source_revision(doc),
        "document_sha256": doc["document_sha256"],
    }


def prepare_repair(params: dict, ctx: Context) -> dict:
    from sonoranlab.api.tools.deterministic import _reference, profile_for, resolve_input_dir
    from sonoranlab.repair import workspace
    from sonoranlab.repair.compare import read_json
    from sonoranlab.repair.confine import repair_root

    profile = profile_for(params, ctx)
    parent, _ = _reference(params["parent_job"], ctx)
    parent = resolve_input_dir(parent if parent.is_dir() else parent.parent, ctx)
    out = invoke(repair_root) / f"repair-{uuid4().hex}"
    invoke(workspace.prepare, parent, profile, params["character"], params["anim"], out)
    doc = read_json(out / "repair.json")
    return {
        "repair_id": out.name,
        "state": "prepared",
        "identity": identity(out, doc),
        "staged_source_paths": [str(out / "source" / name) for name in sorted(doc["source"])],
        "next_steps": {
            "owner_authorization": f"sonoranlab repair authorize {out} --spec <owner-auth.json>",
            "contained_editor_worker": "Follow docs/runbooks/repair.md and the Phase 3.5 contained-worker "
            "procedure in benchmark/results/2026-10-20-phase3.5/dyl110-trials.md; "
            "one worker through the broker, checkpoint and stop before finalize_repair.",
        },
    }


def inspect_repair(params: dict, ctx: Context) -> dict:
    from sonoranlab.repair.authorization import open_private
    from sonoranlab.repair.compare import read_json
    from sonoranlab.repair.workspace import inspect

    repair = repair_path(params["repair_id"])
    info = invoke(inspect, repair)
    state_path = repair / "state.json"
    state = read_json(state_path) if state_path.exists() or state_path.is_symlink() else {}
    auth_path = repair / "authorization.json"
    auth = None
    if auth_path.exists() or auth_path.is_symlink():
        auth = {**read_json(auth_path), "sha256": sha256_file(auth_path)}
        auth["bound"] = auth["sha256"] == state.get("authorization_sha256")
    held = False
    lease = repair / "session/lease.lock"
    if lease.exists() or lease.is_symlink():
        fd = invoke(open_private, lease, os.O_RDONLY | os.O_NONBLOCK)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held = True
        finally:
            os.close(fd)
    journal = repair / "session/ops.jsonl"
    summary = {"events": 0, "event_counts": {}, "sha256": None, "committed_sha256": state.get("log_sha256")}
    if journal.exists() or journal.is_symlink():
        fd = invoke(open_private, journal, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd) as stream:
            counts = Counter(json.loads(line).get("event", "request") for line in stream)
        summary.update(events=sum(counts.values()), event_counts=dict(counts), sha256=sha256_file(journal))
    return {
        "repair_id": repair.name,
        "state": info["state"],
        "identity": identity(repair, info["record"]),
        "authorization": auth,
        "session": {"lease_held": held, "state": state.get("session_state")},
        "journal": summary,
    }


def inspect_repair_diff(params: dict, ctx: Context) -> dict:
    from sonoranlab.repair.authorization import validate
    from sonoranlab.repair.compare import diff, read_json

    repair = repair_path(params["repair_id"])
    result = invoke(diff, repair)
    doc = read_json(repair / "repair.json")
    auth_path = repair / "authorization.json"
    masks = {}
    if auth_path.exists() or auth_path.is_symlink():
        state = read_json(repair / "state.json")
        if sha256_file(auth_path) != state.get("authorization_sha256"):
            raise ToolError("refused", "authorization_hash_mismatch")
        masks = invoke(validate, read_json(auth_path), doc, read_json(repair / "mapping.json"))
    unauthorized = []
    for number, frame in result["frames"].items():
        changes = frame["changed"]
        outside = [[x, y] for x, y in changes if number not in masks or not masks[number][y, x]]
        if outside:
            unauthorized.append({"frame": number, "count": len(outside), "coordinates": outside[:2000]})
        for key in ("changed", "invalid_alpha", "off_palette"):
            frame[f"{key}_count"] = len(frame[key])
            frame[key] = frame[key][:2000]
    return {"repair_id": repair.name, **result, "unauthorized_changes": unauthorized}


def finalize_repair(params: dict, ctx: Context) -> dict:
    from sonoranlab.jobs import manager
    from sonoranlab.jobs.kinds.repair import FinalizeRepair

    # Resolve the repository profile here; workers resolve it again from their Context.
    normalized, _ = FinalizeRepair(ctx).validate(params)
    return manager.submit("finalize_repair", normalized)


def abort_repair(params: dict, ctx: Context) -> dict:
    from sonoranlab.repair.session import abort

    return invoke(abort, repair_path(params["repair_id"]))


def schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


ID = {"type": "string", "pattern": f"^{REPAIR_ID}$"}
SLUG = {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]*$", "maxLength": 128}
TOOLS = tuple(
    Tool(name, description, inputs, read_only, kind, partial(invoke, handler), capability_gate=editor_available)
    for name, description, inputs, read_only, kind, handler in (
        (
            "prepare_repair",
            "Stage a registered parent for owner authorization and a contained editor worker; never start a session.",
            schema(
                {
                    "project": SLUG,
                    "character": SLUG,
                    "anim": SLUG,
                    "parent_job": {"type": "string", "minLength": 1, "maxLength": 4096},
                }
            ),
            False,
            "sync",
            prepare_repair,
        ),
        (
            "inspect_repair",
            "Read repair identity, recorded authorization, writer lease and journal summary without edits.",
            schema({"repair_id": ID}),
            True,
            "sync",
            inspect_repair,
        ),
        (
            "inspect_repair_diff",
            "Read the latest exported pixel diff, capped at 2000 coordinates per frame with full counts.",
            schema({"repair_id": ID}),
            True,
            "sync",
            inspect_repair_diff,
        ),
        (
            "finalize_repair",
            "Queue validation/export/check into a new child job requiring fresh owner review.",
            schema({"repair_id": ID}),
            False,
            "job",
            finalize_repair,
        ),
        (
            "abort_repair",
            "Abort an admitted repair using the broker writer lease, retaining its evidence.",
            schema({"repair_id": ID}),
            False,
            "sync",
            abort_repair,
        ),
    )
)
