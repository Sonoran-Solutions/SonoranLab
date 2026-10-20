"""Production review is bound to the exact reviewed artifact; it cannot make a failed or altered job usable."""

import json
import shutil

import pytest
from test_pipeline import process, raw_set, toy_profile

from sonoranlab.artifacts import sha256_file
from sonoranlab.cli import main
from sonoranlab.review import production_usable, record_review, review_problems

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


@pytest.mark.parametrize("minutes", [None, -1, float("nan"), float("inf")])
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
