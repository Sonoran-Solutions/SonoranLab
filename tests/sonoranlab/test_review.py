"""Production review is bound to the exact reviewed artifact; it cannot make a failed or altered job usable."""

import builtins
import io
import json
import os
import shutil
import stat
from pathlib import Path

import pytest
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
