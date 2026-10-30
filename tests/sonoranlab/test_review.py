"""Production review is bound to the exact reviewed artifact; it cannot make a failed or altered job usable."""

import builtins
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from test_pipeline import process, raw_set, toy_profile

from sonoranlab.artifacts import sha256_file
from sonoranlab.cli import main
from sonoranlab.review import production_usable, record_review, review_errors, review_problems

INTERVAL = {"started_at": "2026-10-19T10:00:00+00:00", "ended_at": "2026-10-19T10:05:00+00:00"}
PASS = {"verdict": "pass", "reviewer": "Dylan", "cleanup_minutes": 5, **INTERVAL}


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """A real, technically passing job: pipeline.run on the synthetic toy profile/frames (built once, it is slow)."""
    root = tmp_path_factory.mktemp("real")
    assert process(raw_set(root), toy_profile(root), root / "job") == 0
    return root / "job"


@pytest.fixture
def job(template, tmp_path):
    return shutil.copytree(template, tmp_path / "job")


def exported(job):
    return sorted(p for p in (job / "export").rglob("*") if p.is_file())


def test_record_review_binds_artifact_and_production_usable(job):
    assert not production_usable(job)
    path = record_review(job, notes="weapon repaired", **PASS)
    doc = json.loads(path.read_text())
    assert doc["schema"] == "sonoranlab.review/2" and doc["notes"] == "weapon repaired"
    assert {k: doc[k] for k in PASS} == PASS
    binds = doc["binds"]
    assert binds["report_sha256"] == sha256_file(job / "report.json")
    assert binds["manifest_sha256"] == sha256_file(job / "manifest.json")
    assert binds["contact_sha256"] == sha256_file(job / "contact.png") and binds["job"] == str(job.resolve())
    assert binds["export"] == {p.relative_to(job).as_posix(): sha256_file(p) for p in exported(job)} != {}
    assert production_usable(job) and review_problems(job) == []
    before = path.read_bytes()
    with pytest.raises(ValueError, match="exists"):
        record_review(job, **{**PASS, "verdict": "fail"})
    assert path.read_bytes() == before


@pytest.mark.parametrize("relocation", ["copy", "move"])
def test_relocated_job_requires_a_new_review(job, relocation):
    record_review(job, **PASS)
    relocated = job.parent / "relocated"
    if relocation == "copy":
        shutil.copytree(job, relocated)
        assert review_errors(job) == []
    else:
        job.rename(relocated)
    assert any(e["code"] == "REVIEW_BINDING_INVALID" and e["field"] == "binds.job" for e in review_errors(relocated))
    assert not production_usable(relocated)
    old = relocated / "review.old.json"
    (relocated / "review.json").rename(old)
    record_review(relocated, supersedes=old, **PASS)
    assert review_errors(relocated) == []


@pytest.mark.parametrize("value", [None, 42, [], "wrong-path"])
def test_missing_or_invalid_job_binding_is_refused(job, value):
    path = record_review(job, **PASS)
    review = json.loads(path.read_text())
    if value is None:
        del review["binds"]["job"]
    else:
        review["binds"]["job"] = value
    path.write_text(json.dumps(review))
    assert any(e["code"] == "REVIEW_BINDING_INVALID" and e["field"] == "binds.job" for e in review_errors(job))
    assert not production_usable(job)


@pytest.mark.parametrize("declared", [False, True])
def test_repair_manifest_is_bound_outside_manifest_outputs(job, declared):
    manifest_path = job / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if declared:
        manifest["repair"] = {"manifest": "repair-manifest.json"}
        manifest_path.write_text(json.dumps(manifest))
    sidecar = job / "repair-manifest.json"
    sidecar.write_text('{"authorization": "owner", "operations": []}')
    path = record_review(job, **PASS)
    assert sidecar.name not in manifest["outputs"]
    assert json.loads(path.read_text())["binds"]["repair_manifest_sha256"] == sha256_file(sidecar)
    assert review_errors(job) == [] and production_usable(job)


@pytest.mark.parametrize(
    "change", ["modified", "deleted", "replaced", "added", "symlink", "dangling", "directory", "fifo"]
)
def test_repair_manifest_changes_are_refused(job, change):
    sidecar = job / "repair-manifest.json"
    if change != "added":
        sidecar.write_text('{"operations": []}')
    record_review(job, **PASS)
    if change == "modified":
        sidecar.write_text('{"operations": ["unapproved"]}')
    elif change == "added":
        sidecar.write_text('{"operations": []}')
    else:
        old = job / "repair-manifest.old.json"
        sidecar.rename(old)
        if change == "replaced":
            sidecar.write_text('{"operations": ["replacement"]}')
        elif change in ("symlink", "dangling"):
            sidecar.symlink_to(old if change == "symlink" else job / "missing.json")
        elif change == "directory":
            sidecar.mkdir()
        elif change == "fifo":
            os.mkfifo(sidecar)
    assert any(
        e["code"] == "REVIEW_BINDING_INVALID" and e["field"] == "binds.repair_manifest_sha256"
        for e in review_errors(job)
    )
    assert not production_usable(job)


@pytest.mark.parametrize("remove", [False, True], ids=["present", "deleted"])
def test_declared_repair_without_binding_is_refused(job, remove):
    manifest_path = job / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repair"] = {"manifest": "repair-manifest.json"}
    manifest_path.write_text(json.dumps(manifest))
    sidecar = job / "repair-manifest.json"
    sidecar.write_text("{}")
    path = record_review(job, **PASS)
    review = json.loads(path.read_text())
    del review["binds"]["repair_manifest_sha256"]
    path.write_text(json.dumps(review))
    if remove:
        sidecar.unlink()
    assert any(
        e["code"] == "REVIEW_BINDING_INVALID" and e["field"] == "binds.repair_manifest_sha256"
        for e in review_errors(job)
    )
    assert not production_usable(job)


@pytest.mark.parametrize("kind", ["missing", "symlink", "dangling", "directory", "fifo"])
def test_record_refuses_declared_repair_without_regular_sidecar(job, kind):
    manifest_path = job / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repair"] = {"manifest": "repair-manifest.json"}
    manifest_path.write_text(json.dumps(manifest))
    sidecar = job / "repair-manifest.json"
    if kind in ("symlink", "dangling"):
        sidecar.symlink_to(job / ("manifest.json" if kind == "symlink" else "missing.json"))
    elif kind == "directory":
        sidecar.mkdir()
    elif kind == "fifo":
        os.mkfifo(sidecar)
    with pytest.raises(ValueError, match=r"repair-manifest\.json"):
        record_review(job, **PASS)
    assert not (job / "review.json").exists()


def test_changed_export_file_invalidates_and_is_named(job):
    record_review(job, **PASS)
    target = exported(job)[0]
    target.write_bytes(target.read_bytes() + b"x")
    assert not production_usable(job)
    assert any(target.relative_to(job).as_posix() in p for p in review_problems(job))


def test_changed_report_manifest_extra_export_or_missing_contact_invalidate(job):
    record_review(job, **PASS)
    assert production_usable(job)
    extra = job / "export" / "extra.png"
    extra.write_bytes(b"x")
    assert not production_usable(job) and any("extra.png" in p for p in review_problems(job))
    extra.unlink()
    assert production_usable(job)
    contact = (job / "contact.png").read_bytes()
    (job / "contact.png").unlink()
    assert not production_usable(job)
    (job / "contact.png").write_bytes(contact)
    removed = exported(job)[0]
    data = removed.read_bytes()
    removed.unlink()
    assert not production_usable(job)
    removed.write_bytes(data)
    assert production_usable(job)
    report = json.loads((job / "report.json").read_text())
    (job / "report.json").write_text(json.dumps({**report, "note": 1}))  # still passes, but not what was reviewed
    assert not production_usable(job)


def test_changed_manifest_invalidates(job):
    record_review(job, **PASS)
    (job / "manifest.json").write_text((job / "manifest.json").read_text() + " ")
    assert not production_usable(job)


def test_changed_raw_invalidates(job):
    record_review(job, **PASS)
    raw = next((job / "raw").iterdir())
    raw.chmod(0o644)
    raw.write_bytes(b"changed")
    assert not production_usable(job) and any("raw/" in p for p in review_problems(job))


def test_fail_review_is_recorded_but_not_usable(job):
    path = record_review(job, verdict="fail", reviewer="Dylan", **INTERVAL)
    assert json.loads(path.read_text())["cleanup_minutes"] is None
    assert not production_usable(job)


def test_legacy_schemaless_review_is_not_usable(job):
    (job / "review.json").write_text(json.dumps({"verdict": "pass", "reviewer": "Dylan", "cleanup_minutes": 1}))
    assert not production_usable(job) and any("schema" in p for p in review_problems(job))


def test_report_only_job_dir_is_refused_for_missing_provenance(tmp_path):
    (tmp_path / "report.json").write_text(json.dumps({"pass": True}))
    with pytest.raises(ValueError, match="manifest"):
        record_review(tmp_path, **PASS)
    assert not (tmp_path / "review.json").exists() and not production_usable(tmp_path)


@pytest.mark.parametrize("minutes", [None, -1, float("nan"), float("inf"), float("-inf"), "5", "NaN", True])
def test_passing_review_requires_non_negative_finite_cleanup(job, minutes):
    with pytest.raises(ValueError, match="cleanup_minutes"):
        record_review(job, **{**PASS, "cleanup_minutes": minutes})
    assert not (job / "review.json").exists()


@pytest.mark.parametrize("report", ['{"pass": false}', "{}", '{"pass": 1}', "[]", "bad json", None])
def test_refuses_unvalidated_job(job, report):
    path = job / "report.json"
    if report is None:
        path.unlink()
    else:
        path.write_text(report)
    with pytest.raises(ValueError, match="passing report"):
        record_review(job, **PASS)
    assert not (job / "review.json").exists() and not production_usable(job)


@pytest.mark.parametrize(
    "edit",
    [
        lambda m: m["result"].update({"pass": False}),
        lambda m: m["result"].update(exported=False),
        lambda m: m["outputs"].pop("report.json"),
        lambda m: [m["outputs"].pop(k) for k in [k for k in m["outputs"] if k.startswith("export/")]],
        lambda m: m["outputs"].update({"contact.png": "0" * 64}),
        lambda m: m["inputs"].update({"ghost.png": "0" * 64}),
    ],
    ids=["not-pass", "not-exported", "no-report", "no-export", "bad-contact-hash", "raw-mismatch"],
)
def test_refuses_inconsistent_manifest(job, edit):
    manifest = json.loads((job / "manifest.json").read_text())
    edit(manifest)
    (job / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        record_review(job, **PASS)
    assert not (job / "review.json").exists()


def test_refuses_export_hash_mismatch_with_manifest(job):
    exported(job)[0].write_bytes(b"tampered before review")
    with pytest.raises(ValueError, match="sha256"):
        record_review(job, **PASS)


def test_supersedes_records_old_review_and_carried_from(job):
    old = job / "review.json"
    old.write_text(json.dumps({"verdict": "pass", "reviewer": "Old", **INTERVAL}))
    aside = job / "review.legacy.json"
    old.rename(aside)
    path = record_review(job, supersedes=aside, carried_from="approved 2026-10-01 in chat", **PASS)
    doc = json.loads(path.read_text())
    assert doc["carried_from"] == "approved 2026-10-01 in chat"
    assert doc["supersedes"] == {
        "path": str(aside),
        "sha256": sha256_file(aside),
        "verdict": "pass",
        "reviewer": "Old",
        **INTERVAL,
    }
    assert aside.exists() and production_usable(job)


def test_supersedes_must_exist(job):
    with pytest.raises(ValueError, match="superseded"):
        record_review(job, supersedes=job / "nope.json", **PASS)
    assert not (job / "review.json").exists()


def test_invalid_metadata_and_report_symlink(job):
    for kwargs in [
        {"verdict": "yes"},
        {"reviewer": " "},
        {"started_at": "yesterday"},
        {"ended_at": "2026-10-19T09:00:00+00:00"},
    ]:
        with pytest.raises(ValueError):
            record_review(job, **{**PASS, **kwargs})
    target = job / "other.json"
    (job / "report.json").rename(target)
    (job / "report.json").symlink_to(target)
    with pytest.raises(ValueError):
        record_review(job, **PASS)


def test_review_cli(job, capsys):
    argv = ["review", "record", str(job), "--verdict", "pass", "--reviewer", "Dylan"]
    argv += ["--started", INTERVAL["started_at"], "--ended", INTERVAL["ended_at"]]
    assert main(["review", "check", str(job)]) == 1
    assert "review.json" in capsys.readouterr().out
    assert main(argv) == 2
    assert "cleanup_minutes" in capsys.readouterr().err
    assert main([*argv, "--cleanup-minutes", "0", "--carried-from", "chat"]) == 0
    assert production_usable(job) and main(["review", "check", str(job)]) == 0
    assert main([*argv, "--cleanup-minutes", "0"]) == 2
    target = exported(job)[0]
    target.write_bytes(b"edited")
    capsys.readouterr()
    assert main(["review", "check", str(job)]) == 1
    assert target.name in capsys.readouterr().out


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("schema", None, "REVIEW_SCHEMA_INVALID"),
        ("schema", "sonoranlab.review/99", "REVIEW_SCHEMA_INVALID"),
        ("reviewer", "", "REVIEW_REVIEWER_INVALID"),
        ("reviewer", " \t\n", "REVIEW_REVIEWER_INVALID"),
        ("reviewer", None, "REVIEW_REVIEWER_INVALID"),
        ("reviewer", 42, "REVIEW_REVIEWER_INVALID"),
        ("reviewer", [], "REVIEW_REVIEWER_INVALID"),
        ("verdict", None, "REVIEW_VERDICT_INVALID"),
        ("verdict", "approved", "REVIEW_VERDICT_INVALID"),
        ("verdict", [], "REVIEW_VERDICT_INVALID"),
        ("verdict", "fail", "REVIEW_NOT_PASS"),
        ("started_at", "yesterday", "REVIEW_TIMESTAMP_INVALID"),
        ("started_at", "2026-10-19T10:00:00", "REVIEW_TIMESTAMP_INVALID"),
        ("started_at", "2026-10-19X10:00:00Z", "REVIEW_TIMESTAMP_INVALID"),
        ("started_at", None, "REVIEW_TIMESTAMP_INVALID"),
        ("ended_at", "not-a-date", "REVIEW_TIMESTAMP_INVALID"),
        ("ended_at", "2026-10-19T10:05:00", "REVIEW_TIMESTAMP_INVALID"),
        ("ended_at", None, "REVIEW_TIMESTAMP_INVALID"),
        ("ended_at", 42, "REVIEW_TIMESTAMP_INVALID"),
        ("ended_at", "2026-10-19T09:59:59Z", "REVIEW_INTERVAL_INVALID"),
        ("ended_at", "2026-10-19T10:05:00+01:00", "REVIEW_INTERVAL_INVALID"),
        ("cleanup_minutes", None, "REVIEW_CLEANUP_INVALID"),
        ("cleanup_minutes", "NaN", "REVIEW_CLEANUP_INVALID"),
        ("cleanup_minutes", "inf", "REVIEW_CLEANUP_INVALID"),
        ("cleanup_minutes", -1, "REVIEW_CLEANUP_INVALID"),
        ("cleanup_minutes", "5", "REVIEW_CLEANUP_INVALID"),
        ("cleanup_minutes", True, "REVIEW_CLEANUP_INVALID"),
        ("cleanup_minutes", [], "REVIEW_CLEANUP_INVALID"),
        ("cleanup_minutes", float("nan"), "REVIEW_UNREADABLE"),
        ("cleanup_minutes", float("inf"), "REVIEW_UNREADABLE"),
        ("cleanup_minutes", float("-inf"), "REVIEW_UNREADABLE"),
    ],
)
def test_edited_review_metadata_is_rejected(job, field, value, code):
    path = record_review(job, **PASS)
    review = json.loads(path.read_text())
    review[field] = value
    path.write_text(json.dumps(review))
    errors = review_errors(job)
    assert errors[0]["code"] == code
    assert errors[0]["field"] == ("review.json" if code == "REVIEW_UNREADABLE" else field)
    assert not production_usable(job)


@pytest.mark.parametrize("field", ["schema", "reviewer", "verdict", "started_at", "ended_at", "cleanup_minutes"])
def test_missing_review_metadata_is_rejected(job, field):
    path = record_review(job, **PASS)
    review = json.loads(path.read_text())
    del review[field]
    path.write_text(json.dumps(review))
    code = {
        "schema": "REVIEW_SCHEMA_INVALID",
        "reviewer": "REVIEW_REVIEWER_INVALID",
        "verdict": "REVIEW_VERDICT_INVALID",
        "started_at": "REVIEW_TIMESTAMP_INVALID",
        "ended_at": "REVIEW_TIMESTAMP_INVALID",
        "cleanup_minutes": "REVIEW_CLEANUP_INVALID",
    }[field]
    assert any(e["code"] == code and e["field"] == field for e in review_errors(job))
    assert not production_usable(job)


@pytest.mark.parametrize("minutes", [0, 0.0])
@pytest.mark.parametrize(
    "timestamp", [INTERVAL["started_at"], "2026-10-19T10:00:00.1Z", "2026-10-19T10:00:00.123456+23:59"]
)
def test_zero_cleanup_and_equal_timestamps_pass(job, minutes, timestamp):
    record_review(job, **{**PASS, "cleanup_minutes": minutes, "started_at": timestamp, "ended_at": timestamp})
    assert review_errors(job) == []
    assert production_usable(job)


@pytest.mark.parametrize("target", ["export", "report.json", "manifest.json", "contact.png", "raw", "processed"])
@pytest.mark.parametrize("remove", [False, True], ids=["changed", "missing"])
def test_production_usable_rechecks_all_artifacts(job, target, remove):
    record_review(job, **PASS)
    assert production_usable(job)
    path = job / target
    if path.is_dir():
        path = next(p for p in path.rglob("*") if p.is_file())
    if remove:
        path.unlink()
    else:
        path.chmod(0o644)
        path.write_bytes(path.read_bytes() + b" ")
    errors = review_errors(job)
    assert errors and all(e["code"] == "REVIEW_BINDING_INVALID" for e in errors)
    assert not production_usable(job)


@pytest.mark.parametrize("remove", [False, True], ids=["changed", "missing"])
def test_profile_binding_is_rechecked(job, remove):
    manifest_path = job / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    profile = job / "profile.yaml"
    shutil.copyfile(manifest["profile"]["path"], profile)
    manifest["profile"]["path"] = str(profile)
    manifest_path.write_text(json.dumps(manifest))
    record_review(job, **PASS)
    assert production_usable(job)
    if remove:
        profile.unlink()
    else:
        profile.write_text(profile.read_text() + "\n")
    errors = review_errors(job)
    assert errors[0]["code"] == "REVIEW_BINDING_INVALID" and "profile" in errors[0]["message"]
    assert not production_usable(job)


def test_review_cli_exposes_structured_errors(job, capsys):
    path = record_review(job, **PASS)
    review = json.loads(path.read_text())
    review["reviewer"] = None
    path.write_text(json.dumps(review))
    assert main(["review", "check", str(job)]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "code": "REVIEW_REVIEWER_INVALID",
        "field": "reviewer",
        "message": "reviewer must be a non-empty string",
    }


@pytest.mark.parametrize(
    "target",
    [
        "export",
        "export/hidden",
        "export/nested/hidden",
        "report.json",
        "manifest.json",
        "review.json",
        "raw",
        "processed",
    ],
)
def test_symlink_entries_are_refused(job, target):
    record_review(job, **PASS)
    path = job / target
    aside = job.parent / "linked"
    if path.exists():
        path.rename(aside)
    else:
        aside.mkdir()
        (aside / "unreviewed.txt").write_text("hidden")
        path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(aside, target_is_directory=aside.is_dir())
    errors = review_errors(job)
    code = "REVIEW_UNREADABLE" if target == "review.json" else "REVIEW_BINDING_INVALID"
    assert any(e["code"] == code for e in errors)
    assert not production_usable(job)


@pytest.mark.parametrize("kind", ["fifo", "socket", "device", "directory"])
@pytest.mark.parametrize(
    "target",
    [
        "export/nested/extra",
        "export",
        "report.json",
        "manifest.json",
        "review.json",
        "contact.png",
        "raw",
        "processed",
        "profile",
    ],
)
def test_non_regular_files_are_refused_without_opening(job, monkeypatch, target, kind):
    # Give the job its own profile so replacing it cannot damage the shared fixture.
    manifest_path = job / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    profile = job / "profile.yaml"
    shutil.copyfile(manifest["profile"]["path"], profile)
    manifest["profile"]["path"] = str(profile)
    manifest_path.write_text(json.dumps(manifest))
    record_review(job, **PASS)
    path = profile if target == "profile" else job / target
    if target in ("raw", "processed"):
        path = next(p for p in path.rglob("*") if p.is_file())
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "fifo":
        os.mkfifo(path)
    elif kind in ("socket", "device"):
        # Socket binds and mknod are sandbox-restricted; emulate their lstat modes.
        path.touch()
        original_lstat = Path.lstat

        def special_lstat(self, *args, **kwargs):
            info = original_lstat(self, *args, **kwargs)
            mode = stat.S_IFSOCK if kind == "socket" else stat.S_IFCHR
            return os.stat_result((mode, *info[1:])) if self == path else info

        monkeypatch.setattr(Path, "lstat", special_lstat)
    else:
        path.mkdir()
        if target.startswith("export/"):
            # Real directories are allowed in export; all other cases replace hashed files.
            os.mkfifo(path / "hidden")

    def guard(opener):
        def checked(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)):
                assert stat.S_ISREG(Path(file).lstat().st_mode), f"attempted to open {file}"
            return opener(file, *args, **kwargs)

        return checked

    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    monkeypatch.setattr(io, "open", guard(io.open))
    errors = review_errors(job)
    code = "REVIEW_UNREADABLE" if target == "review.json" else "REVIEW_BINDING_INVALID"
    assert any(e["code"] == code for e in errors)
    assert not production_usable(job)


@pytest.mark.parametrize("target", ["review.json", "manifest.json", "report.json"])
@pytest.mark.parametrize("invalid", ["nested", "oversized", "duplicate", "nested-duplicate"])
def test_hostile_json_is_a_structured_refusal(job, target, invalid):
    record_review(job, **PASS)
    path = job / target
    if invalid == "nested":
        data = "[" * 10000 + "0" + "]" * 10000
    elif invalid == "oversized":
        data = path.read_text() + " " * (16 * 1024 * 1024)
    else:
        key, first, last = ("verdict", '"fail"', '"pass"') if target == "review.json" else ("pass", "false", "true")
        duplicate = f'"{key}":{first},"{key}":{last}'
        if invalid == "nested-duplicate":
            duplicate = f'"duplicate":{{{duplicate}}}'
        data = path.read_text().replace("{", "{" + duplicate + ",", 1)
    path.write_text(data)
    if target != "review.json":
        # Rebind hashes so parsing must refuse even when the bytes are otherwise approved.
        if target == "report.json":
            manifest = json.loads((job / "manifest.json").read_text())
            manifest["outputs"]["report.json"] = sha256_file(path)
            (job / "manifest.json").write_text(json.dumps(manifest))
        review = json.loads((job / "review.json").read_text())
        review["binds"]["manifest_sha256"] = sha256_file(job / "manifest.json")
        review["binds"]["report_sha256"] = sha256_file(job / "report.json")
        (job / "review.json").write_text(json.dumps(review))
    errors = review_errors(job)
    code = "REVIEW_UNREADABLE" if target == "review.json" else "REVIEW_BINDING_INVALID"
    assert any(e["code"] == code for e in errors)
    assert not production_usable(job)


@pytest.mark.parametrize("offset", ["+00:60", "-00:60", "+00:00:60", "+24:00", "+0000"])
def test_normalized_or_nonstandard_timestamp_offsets_are_refused(job, offset):
    value = "2026-10-19T10:00:00" + offset
    with pytest.raises(ValueError, match="REVIEW_TIMESTAMP_INVALID"):
        record_review(job, **{**PASS, "started_at": value})
    path = record_review(job, **PASS)
    doc = json.loads(path.read_text())
    doc["started_at"] = value
    path.write_text(json.dumps(doc))
    assert any(e["code"] == "REVIEW_TIMESTAMP_INVALID" for e in review_errors(job))
    assert not production_usable(job)


@pytest.mark.parametrize("field", ["notes", "carried_from", "supersedes"])
@pytest.mark.parametrize("value", [float("nan"), 42, [], {}])
def test_writer_and_reader_refuse_invalid_optional_metadata(job, field, value):
    with pytest.raises(ValueError, match=field):
        record_review(job, **{**PASS, field: value})
    assert not (job / "review.json").exists()
    path = record_review(job, **PASS)
    doc = json.loads(path.read_text())
    doc[field] = value
    path.write_text(json.dumps(doc))
    assert any(e["code"] in ("REVIEW_UNREADABLE", "REVIEW_METADATA_INVALID") for e in review_errors(job))
    assert not production_usable(job)


def test_superseded_metadata_is_validated_before_writing(job):
    old = job / "review.old.json"
    old.write_text('{"reviewer":1e400}')
    with pytest.raises(ValueError, match="supersedes"):
        record_review(job, supersedes=old, **PASS)
    assert not (job / "review.json").exists()
    path = record_review(job, **PASS)
    doc = json.loads(path.read_text())
    doc["supersedes"] = {"reviewer": 1e400}
    path.write_text(json.dumps(doc).replace("Infinity", "1e400"))
    assert any(e["code"] == "REVIEW_METADATA_INVALID" for e in review_errors(job))
    assert not production_usable(job)


def test_oversized_review_is_not_created(job):
    with pytest.raises(ValueError, match="JSON exceeds"):
        record_review(job, notes="x" * (16 * 1024 * 1024), **PASS)
    assert not (job / "review.json").exists()


def test_overflowing_integer_cleanup_is_a_structured_refusal(job):
    minutes = 10**400
    with pytest.raises(ValueError, match="REVIEW_CLEANUP_INVALID"):
        record_review(job, **{**PASS, "cleanup_minutes": minutes})
    assert not (job / "review.json").exists()
    path = record_review(job, **PASS)
    doc = json.loads(path.read_text())
    doc["cleanup_minutes"] = minutes
    path.write_text(json.dumps(doc))
    assert any(e["code"] == "REVIEW_CLEANUP_INVALID" for e in review_errors(job))
    assert not production_usable(job)


def test_shared_hasher_and_writer_stay_permissive_for_the_pipeline(tmp_path):
    """Review strictness must not leak into the pipeline: symlinked ancestors and inf scores are legitimate there."""
    import json as _json

    from sonoranlab.artifacts import sha256_file as pipeline_sha256
    from sonoranlab.artifacts import write_json as pipeline_write_json

    real = tmp_path / "real"
    real.mkdir()
    (real / "a.png").write_bytes(b"x")
    (tmp_path / "alias").symlink_to(real)
    assert pipeline_sha256(tmp_path / "alias" / "a.png") == pipeline_sha256(real / "a.png")
    out = pipeline_write_json(tmp_path, "sweep.json", {"worst_ratio": float("inf")})
    assert _json.loads(out.read_text())["worst_ratio"] == float("inf")


@pytest.mark.parametrize("name", ["\u200b", "\u200b \u2060", "\t\n"])
def test_invisible_reviewer_is_refused(job, name):
    with pytest.raises(ValueError, match="REVIEW_REVIEWER_INVALID"):
        record_review(job, **{**PASS, "reviewer": name})


def _git_profile(job, edit):
    """Move the job's profile into a git repo, bind the manifest to that commit, then apply `edit` to the file."""
    manifest_path = job / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    repo = job.parent / "profile-repo"
    repo.mkdir()
    doc = yaml.safe_load(Path(manifest["profile"]["path"]).read_text())
    doc["characters"]["villain"] = dict(doc["characters"][manifest["character"]])
    profile = repo / "toy.yaml"
    profile.write_text(yaml.safe_dump(doc, sort_keys=False))
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "toy.yaml"], check=True)
    subprocess.run([*git, "commit", "-qm", "p"], check=True)
    rev = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    manifest["profile"] = {"path": str(profile), "sha256": hashlib.sha256(profile.read_bytes()).hexdigest()}
    manifest["git"] = rev
    manifest_path.write_text(json.dumps(manifest))
    record_review(job, **PASS)
    assert production_usable(job)
    edit(doc, manifest["character"])
    profile.write_text(yaml.safe_dump(doc, sort_keys=False))
    return manifest_path, manifest


def test_other_characters_row_change_keeps_binding(job):
    _git_profile(job, lambda doc, me: doc["characters"]["villain"].update(max_attempts_per_animation=6))
    assert review_errors(job) == [] and production_usable(job)


def test_own_admission_only_attempt_limit_keeps_binding(job):
    _git_profile(job, lambda doc, me: doc["characters"][me].update(max_attempts_per_animation=6))
    assert review_errors(job) == [] and production_usable(job)


def test_own_row_override_of_another_clip_keeps_binding(job):
    """Gun cast/death (2026-10-29): adding the row's overrides for other clips keeps this clip's job bound; an override
    of the job's own clip breaks it (next test)."""
    over = {"death": {"track_default": "E", "validator_mode": "fall"}}
    _git_profile(job, lambda doc, me: doc["characters"][me].update(animations=over))
    assert review_errors(job) == [] and production_usable(job)


def test_own_row_override_of_the_jobs_clip_breaks_binding(job):
    anim = json.loads((job / "report.json").read_text())["animation"]
    _git_profile(job, lambda doc, me: doc["characters"][me].update(animations={anim: {"fps": 7}}))
    assert not production_usable(job)


@pytest.mark.parametrize(
    "edit",
    [
        lambda doc, me: doc["characters"][me].update(cell=doc["characters"][me]["cell"] + 1),
        lambda doc, me: doc["autonomy"].update(max_attempts_per_animation=6),
        lambda doc, me: doc["characters"].pop(me),
    ],
    ids=["own-row", "shared-setting", "own-row-removed"],
)
def test_own_row_or_shared_setting_change_breaks_binding(job, edit):
    _git_profile(job, edit)
    assert not production_usable(job)


@pytest.mark.parametrize("field", ["git", "sha256"])
def test_unverifiable_recorded_profile_breaks_binding(job, field):
    manifest_path, manifest = _git_profile(
        job, lambda doc, me: doc["characters"]["villain"].update(max_attempts_per_animation=6)
    )
    if field == "git":
        manifest["git"] = "0" * 40
    else:
        manifest["profile"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    assert not production_usable(job)


def test_shared_setting_type_change_breaks_binding(job):
    _git_profile(job, lambda doc, me: doc["canvas"].update(gen_scale=float(doc["canvas"]["gen_scale"])))
    assert not production_usable(job)


def test_relabelled_manifest_character_cannot_pick_another_row(job):
    def edit(doc, me):
        doc["characters"][me]["cell"] += 1  # the job's real row changes; the relabel points at an unchanged one

    manifest_path, manifest = _git_profile(job, edit)
    manifest["character"] = "villain"
    manifest_path.write_text(json.dumps(manifest))
    assert not production_usable(job)
    assert any("sha256 differs from manifest" in e["message"] and "profile" in e["message"] for e in review_errors(job))


def _committed_profile(job, monkeypatch):
    """The job's profile committed in a throwaway repo standing in for this repository; the manifest binds it."""
    from sonoranlab import repo_inputs

    manifest_path = job / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    repo = job.parent / "sl-repo"
    (repo / "profiles").mkdir(parents=True)
    profile = repo / "profiles/toy.yaml"
    shutil.copyfile(manifest["profile"]["path"], profile)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "p"], check=True)
    rev = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    manifest["profile"] = {"path": str(profile), "sha256": hashlib.sha256(profile.read_bytes()).hexdigest()}
    manifest["git"] = rev
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(repo_inputs, "_REPO", repo)
    return profile


def test_removed_worktree_profile_binds_through_git_recovery(job, monkeypatch):
    from sonoranlab import repo_inputs

    profile = _committed_profile(job, monkeypatch)
    record_review(job, **PASS)
    profile.unlink()  # the worktree that held it is gone; git still holds the exact bytes
    assert review_errors(job) == [] and production_usable(job)
    monkeypatch.setattr(repo_inputs, "_REPO", job.parent)  # a repository without that blob: nothing to recover
    assert not production_usable(job)
    assert any("profile" in e["message"] and "missing" in e["message"] for e in review_errors(job))


def test_recovery_never_carries_an_approval_to_a_copy(job, monkeypatch):
    profile = _committed_profile(job, monkeypatch)
    record_review(job, **PASS)
    profile.unlink()
    copy = job.parent / "copied-job"
    shutil.copytree(job, copy, symlinks=True)
    assert production_usable(job) and not production_usable(copy)  # the review binds its own job path


def _outside_profile(job, monkeypatch):
    """The recorded profile sits outside this checkout (a removed or foreign worktree); this repository's git holds the
    exact recorded bytes under the same path suffix."""
    committed = _committed_profile(job, monkeypatch)
    outside = job.parent / "old-worktree/profiles/toy.yaml"
    outside.parent.mkdir(parents=True)
    shutil.copyfile(committed, outside)
    manifest_path = job / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["profile"]["path"] = str(outside)
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", str(job))  # the old worktree is outside the read roots
    monkeypatch.setenv("SONORANLAB_STATE", str(job / "state"))
    return outside


def _never_open(monkeypatch, target):
    target = Path(target).resolve()

    def wrap(original):
        def guard(path, *args, **kwargs):
            if isinstance(path, (str, os.PathLike)):
                assert Path(path).resolve() != target, f"{target} opened"
            return original(path, *args, **kwargs)

        return guard

    for name in ("read_text", "read_bytes", "open"):
        monkeypatch.setattr(Path, name, wrap(getattr(Path, name)))
    monkeypatch.setattr(builtins, "open", wrap(builtins.open))


def test_outside_profile_binds_only_through_git_and_is_never_opened(job, monkeypatch):
    profile = _outside_profile(job, monkeypatch)
    record_review(job, **PASS)
    profile.write_text("id: something else entirely\n")  # outside this checkout: must not be read
    _never_open(monkeypatch, profile)
    assert review_errors(job) == [] and production_usable(job)  # bound only through the recorded bytes in git


def test_compose_job_integrity_never_opens_an_outside_profile(job, monkeypatch):
    from sonoranlab.compose import job_integrity

    profile = _outside_profile(job, monkeypatch)
    profile.write_text("id: something else entirely\n")
    _never_open(monkeypatch, profile)
    binds, problems = job_integrity(job)
    assert binds is not None and problems == []


def test_drifted_own_row_inside_the_checkout_still_breaks_binding(job, monkeypatch):
    from sonoranlab import repo_inputs

    _git_profile(job, lambda doc, me: doc["characters"][me].update(cell=doc["characters"][me]["cell"] + 1))
    monkeypatch.setattr(repo_inputs, "_REPO", job.parent / "profile-repo")  # the profile is this checkout's own file
    assert not production_usable(job)
    assert any("sha256 differs from manifest" in e["message"] for e in review_errors(job))


def test_profile_inside_the_read_roots_is_read_in_place_without_git(job, monkeypatch):
    from sonoranlab import repo_inputs

    profile = _outside_profile(job, monkeypatch)
    record_review(job, **PASS)
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", str(job.parent))  # a readable worktree: no git, no subprocess

    def no_git(*args, **kwargs):
        raise AssertionError("git consulted for a readable profile")

    recover = repo_inputs.recover
    monkeypatch.setattr(repo_inputs, "recover", no_git)
    assert production_usable(job)
    monkeypatch.setattr(repo_inputs, "recover", recover)  # a mismatch may consult git (the same-row equivalence)
    doc = yaml.safe_load(profile.read_text())
    doc["alpha"]["threshold"] -= 1
    profile.write_text(yaml.safe_dump(doc))
    assert not production_usable(job)  # read in place, a shared-setting drift breaks the binding
