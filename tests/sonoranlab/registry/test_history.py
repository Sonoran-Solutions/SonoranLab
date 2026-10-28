"""DYL-126: get_character merges durable native jobs and registry imports into one bounded history."""

import json
import os
import shutil

import pytest
from test_records import register, setup, snapshot  # noqa: F401 -- reuse the registry fixtures
from test_review import PASS, exported, template  # noqa: F401
from test_review import job as real_job

from sonoranlab import registry as records
from sonoranlab.api import call
from sonoranlab.api.tools.jobs import list_jobs
from sonoranlab.jobs import store
from sonoranlab.review import record_review

job = real_job


def native(state, job_id, kind, final, *, created, character="hero", project="toy", parent=None, attempt=1, **extra):
    """Write a schema-valid durable record plus its matching journal and output directory."""
    path = state / "jobs" / job_id
    path.mkdir(parents=True)
    output = f"artifacts/{project}/{character}/idle/{job_id}"
    (state / output).mkdir(parents=True, exist_ok=True)
    states = {"queued": ["queued"], "running": ["queued", "running"]}.get(final, ["queued", "running", final])
    error = {"code": "boom", "stage": "generate", "message": "synthetic failure", "retryable": True}
    history, previous = [], None
    for name in states:
        history.append(
            {
                "at": created,
                "from": previous,
                "state": name,
                "stage": "prepare",
                "error": error if name == "failed" else None,
            }
        )
        previous = name
    record = {
        "schema": store.SCHEMA,
        "job_id": job_id,
        "kind": kind,
        "project": project,
        "character": character,
        "anim": "idle",
        "params": {"project": project, "character": character, "anim": "idle"},
        "input_hashes": {},
        "request_fingerprint": "0" * 64,
        "idempotency_key_sha256": None,
        "parent_job_id": parent,
        "attempt": attempt,
        "gpu": False,
        "track": None,
        "need_gib": None,
        "created_at": created,
        "state": final,
        "stage": "prepare",
        "history": history,
        "worker": {"launcher": "popen", "unit": None, "pid": None, "boot_id": None, "started_at": None},
        "output_dir": output,
        "prompts": [],
        "timing": {"queue_wait_s": None, "run_s": None, "per-stage": {}},
        "result": None,
        "error": error if final == "failed" else None,
        "cancel": None,
        **extra,
    }
    if final == "running":
        record["worker"]["started_at"] = created
    (path / "job.json").write_text(json.dumps(record))
    (path / "events.jsonl").write_bytes(b"".join(store.canonical(e) + b"\n" for e in history))
    assert store.read(path) == record
    return state / output


def records_only(state):
    return {k: v for k, v in snapshot(state).items() if not k.endswith(".lock")}


def ids(n):
    return f"job-20261020T00000{n}Z-0000000{n}"


@pytest.fixture
def mixed(setup, job, monkeypatch, tmp_path):  # noqa: F811
    ctx, _, state = setup
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", os.pathsep.join([str(tmp_path), str(job.parent.parent)]))
    register(setup)
    # Imported historical asset, outside STATE.
    history = records.import_job("toy", "hero", str(job), ctx=ctx)
    # Native jobs of every execution state.
    native(state, ids(1), "process", "queued", created="2026-10-20T00:00:01+00:00")
    native(state, ids(2), "animate", "running", created="2026-10-20T00:00:02+00:00")
    native(state, ids(3), "process", "failed", created="2026-10-20T00:00:03+00:00")
    native(state, ids(4), "process", "queued", created="2026-10-20T00:00:04+00:00", parent=ids(3), attempt=2)
    out = native(
        state,
        ids(5),
        "process",
        "succeeded",
        created="2026-10-20T00:00:05+00:00",
        result={"technical": {"validator": "pass", "godot": "not_run"}},
    )
    shutil.copytree(job, out, dirs_exist_ok=True)
    record_out = records.import_job("toy", "hero", str(out), ctx=ctx)
    # A repaired child of the native succeeded job, imported by its native finalize_repair job.
    child = shutil.copytree(job, tmp_path / "repairs/child")
    (child / "review.json").unlink(missing_ok=True)
    manifest = json.loads((child / "manifest.json").read_text())
    manifest["upstream"] = {"parent_job": str(out)}
    (child / "manifest.json").write_text(json.dumps(manifest))
    child_import = records.import_job("toy", "hero", str(child), ctx=ctx)
    native(
        state,
        ids(6),
        "finalize_repair",
        "succeeded",
        created="2026-10-20T00:00:06+00:00",
        result={"artifact_id": child_import["artifact_id"], "child_path": str(child)},
    )
    # Neighbors that must never appear (cross-project, cross-character) or break the read (corrupt).
    native(state, ids(7), "process", "succeeded", created="2026-10-20T00:00:07+00:00", project="other")
    native(state, ids(8), "process", "succeeded", created="2026-10-20T00:00:08+00:00", character="villain")
    (state / "jobs" / ids(9)).mkdir()
    (state / "jobs" / ids(9) / "job.json").write_text("{")
    corrupt = "job-20261020T000000Z-0000000a"
    (state / "jobs" / corrupt).mkdir()
    (state / "jobs" / corrupt / "job.json").write_text(
        json.dumps({"project": "toy", "character": "hero", "created_at": "2026-10-20T00:00:00+00:00"})
    )
    return (
        ctx,
        state,
        job,
        out,
        child,
        {
            "history": history,
            "out": record_out,
            "child": child_import,
            "corrupt": corrupt,
        },
    )


def test_mixed_history_merges_once_with_aliases_lineage_and_isolation(mixed):
    ctx, state, _, out, _, imports = mixed
    with store.lock(state / "jobs" / ids(2) / "lease.lock"):  # A live worker holds the running job's lease.
        before = records_only(state)
        character = call("get_character", {"project": "toy", "character": "hero"}, ctx=ctx)
        # Read-only: no reconciliation write, journal repair, approval or registry change (only lock inodes appear).
        assert records_only(state) == before
    assert character["ok"], character
    result = character["result"]
    entries = {e["id"]: e for e in result["jobs"]}
    # Each logical job once: 6 native + corrupt + historical import; the native-output and child imports merge.
    assert result["job_count"] == len(result["jobs"]) == 8 and not result["jobs_truncated"]
    assert result["unattributed_invalid_jobs"] == 1
    assert [e["id"] for e in result["jobs"]] == [
        imports["corrupt"],
        *map(ids, range(1, 7)),
        imports["history"]["artifact_id"],
    ]
    assert {ids(7), ids(8)}.isdisjoint(entries)
    assert [entries[ids(n)]["state"] for n in range(1, 7)] == [
        "queued",
        "running",
        "failed",
        "queued",
        "succeeded",
        "succeeded",
    ]

    corrupt = entries[imports["corrupt"]]
    assert corrupt["status"] == "invalid" and corrupt["problems"][0]["code"] == "internal"
    assert corrupt["human"] == {"review": "invalid", "production_usable": False}

    historical = entries[imports["history"]["artifact_id"]]
    assert historical["source"] == "import" and historical["status"] == "ok"
    assert historical["aliases"] == [imports["history"]["job_id"]] and historical["state"] is None

    merged = entries[ids(5)]
    assert merged["source"] == "native+import" and merged["kind"] == "process"
    assert merged["aliases"] == sorted({imports["out"]["artifact_id"], imports["out"]["job_id"]} - {ids(5)})
    assert merged["technical"] == {"validator": "pass", "godot": "not_run"}

    repair = entries[ids(6)]
    assert repair["source"] == "native+import" and imports["child"]["artifact_id"] in repair["aliases"]
    assert repair["lineage"]["parent_id"] == ids(5) and repair["lineage"]["parent_job"] == str(out)
    assert merged["lineage"]["child_ids"] == [ids(6)] and merged["lineage"]["repair_children"] == [ids(6)]

    assert entries[ids(3)]["attempt"] == {"number": 1, "parent_job_id": None, "retries": [ids(4)]}
    assert entries[ids(4)]["attempt"] == {"number": 2, "parent_job_id": ids(3), "retries": []}
    assert entries[ids(3)]["error"]["code"] == "boom"

    # Consistency with list_jobs, check_review and get_artifact.
    listed = {j["job_id"] for j in list_jobs({"project": "toy"}, ctx)["jobs"] if j.get("character") == "hero"}
    assert listed == {ids(n) for n in range(1, 7)}
    for entry in result["jobs"]:
        if entry["status"] == "ok":
            for identifier in (entry["id"], *entry["aliases"]):
                review = records.check_review(identifier, ctx=ctx)
                assert review["production_usable"] == entry["human"]["production_usable"]
            assert records.get_artifact(entry["id"], ctx=ctx)

    light = call("list_characters", {"project": "toy"}, ctx=ctx)["result"]["characters"][0]
    assert light["job_count"] == 8 and "review_problems" not in json.dumps(light)


def test_approval_recomputed_and_drift_isolated(mixed):
    ctx, _, job, out, _, imports = mixed

    def entry(identifier):
        jobs = records.get_character("toy", "hero", ctx=ctx)["jobs"]
        return next(e for e in jobs if e["id"] == identifier)

    historical = imports["history"]["artifact_id"]
    assert entry(historical)["human"] == {"review": "none", "production_usable": False}
    record_review(job, **PASS)
    record_review(out, **PASS)
    assert entry(historical)["human"] == {"review": "pass", "production_usable": True}
    assert entry(ids(5))["human"] == {"review": "pass", "production_usable": True}
    exported(out)[0].write_bytes(b"changed export")
    drifted = entry(ids(5))
    assert drifted["status"] == "invalid" and drifted["human"]["production_usable"] is False
    assert any(p["code"] == "import_drift" for p in drifted["problems"])
    assert entry(historical)["human"]["production_usable"]  # Neighbor unaffected.


def test_pagination_is_deterministic_and_bounded(mixed):
    ctx, *_ = mixed
    full = records.get_character("toy", "hero", ctx=ctx)
    pages, offset = [], 0
    while True:
        page = call(
            "get_character", {"project": "toy", "character": "hero", "jobs_offset": offset, "jobs_limit": 3}, ctx=ctx
        )
        assert page["ok"], page
        page = page["result"]
        assert page["job_count"] == full["job_count"] and len(page["jobs"]) <= 3
        pages += [e["id"] for e in page["jobs"]]
        offset += 3
        if not page["jobs_truncated"]:
            break
    assert pages == [e["id"] for e in full["jobs"]]
    beyond = records.get_character("toy", "hero", jobs_offset=50, ctx=ctx)
    assert beyond["jobs"] == [] and not beyond["jobs_truncated"]
    for bad in ({"jobs_limit": 0}, {"jobs_limit": 201}, {"jobs_offset": -1}):
        response = call("get_character", {"project": "toy", "character": "hero", **bad}, ctx=ctx)
        assert response["error"]["code"] == "invalid_params"
