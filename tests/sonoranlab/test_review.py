"""Production review cannot turn a failed technical job into a usable asset."""

import json

import pytest

from sonoranlab.cli import main
from sonoranlab.review import production_usable, record_review

INTERVAL = {"started_at": "2026-10-19T10:00:00+00:00", "ended_at": "2026-10-19T10:05:00+00:00"}


@pytest.fixture
def job(tmp_path):
    (tmp_path / "report.json").write_text(json.dumps({"pass": True}))
    return tmp_path


def test_record_review_and_production_usable(job):
    assert not production_usable(job)
    path = record_review(job, verdict="pass", reviewer="Dylan", cleanup_minutes=5, notes="weapon repaired", **INTERVAL)
    doc = json.loads(path.read_text())
    assert doc == {"verdict": "pass", "reviewer": "Dylan", "cleanup_minutes": 5, "notes": "weapon repaired", **INTERVAL}
    assert production_usable(job)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="exists"):
        record_review(job, verdict="fail", reviewer="Dylan", **INTERVAL)
    assert path.read_bytes() == before
    (job / "report.json").write_text('{"pass": false}')
    assert not production_usable(job)


def test_fail_review_is_recorded_but_not_usable(job):
    path = record_review(job, verdict="fail", reviewer="Dylan", **INTERVAL)
    assert json.loads(path.read_text())["cleanup_minutes"] is None
    assert not production_usable(job)


@pytest.mark.parametrize("minutes", [None, -1, float("nan"), float("inf")])
def test_passing_review_requires_non_negative_finite_cleanup(job, minutes):
    with pytest.raises(ValueError, match="cleanup_minutes"):
        record_review(job, verdict="pass", reviewer="Dylan", cleanup_minutes=minutes, **INTERVAL)
    assert not (job / "review.json").exists()


@pytest.mark.parametrize("report", ['{"pass": false}', "{}", '{"pass": 1}', "[]", "bad json", None])
def test_refuses_unvalidated_job(job, report):
    path = job / "report.json"
    if report is None:
        path.unlink()
    else:
        path.write_text(report)
    with pytest.raises(ValueError, match="passing report"):
        record_review(job, verdict="pass", reviewer="Dylan", cleanup_minutes=0, **INTERVAL)
    assert not (job / "review.json").exists() and not production_usable(job)


def test_invalid_metadata_and_report_symlink(job):
    for kwargs in [
        {"verdict": "yes"},
        {"reviewer": " "},
        {"started_at": "yesterday"},
        {"ended_at": "2026-10-19T09:00:00+00:00"},
    ]:
        fields = {"verdict": "pass", "reviewer": "Dylan", "cleanup_minutes": 0, **INTERVAL, **kwargs}
        with pytest.raises(ValueError):
            record_review(job, **fields)
    target = job / "other.json"
    (job / "report.json").rename(target)
    (job / "report.json").symlink_to(target)
    with pytest.raises(ValueError):
        record_review(job, verdict="pass", reviewer="Dylan", cleanup_minutes=0, **INTERVAL)


def test_review_cli(job, capsys):
    argv = [
        "review",
        "record",
        str(job),
        "--verdict",
        "pass",
        "--reviewer",
        "Dylan",
        "--started",
        INTERVAL["started_at"],
        "--ended",
        INTERVAL["ended_at"],
    ]
    assert main(argv) == 2
    assert "cleanup_minutes" in capsys.readouterr().err
    assert main([*argv, "--cleanup-minutes", "0"]) == 0
    assert production_usable(job)
    assert main([*argv, "--cleanup-minutes", "0"]) == 2
