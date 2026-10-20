"""benchmark/tools/score.py on synthetic manifests, selection.json and report.json files (no images, no game data)."""

import csv
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmark" / "tools"))
import score as S

DATE = "2026-10-18"


def manifest(root: Path, job: str, exit_code: int = 0, wall_s: float = 60, mem: tuple | None = None) -> None:
    d = root / job.split("/", 1)[1]
    d.mkdir(parents=True, exist_ok=True)
    run: dict = {"wall_s": wall_s}
    if mem:
        run["host_mem_available_gib"] = {"before": mem[0], "min": mem[1]}
    (d / "manifest.json").write_text(json.dumps({"job": job, "exit_code": exit_code, "run": run}))


def selection(sdir: Path, attempts: list[dict], **kw) -> str:
    """Writes SET/selection.json, returns its sha256."""
    sdir.mkdir(parents=True, exist_ok=True)
    sel = {"schema": "sonoranlab.selection/1", "attempts": attempts, "selected": {}, "missing_parts": [], **kw}
    (sdir / "selection.json").write_text(json.dumps(sel))
    return hashlib.sha256((sdir / "selection.json").read_bytes()).hexdigest()


def att(job: str, wall_s: float, exit_code: int = 0, selected: bool = True) -> dict:
    return {"job": job, "dir": job.split("/", 1)[1], "part": "grid", "retry": 0, "exit_code": exit_code,
            "wall_s": wall_s, "selected": selected}


def report(path: Path, passed: bool, sel_sha: str | None, sheet: str = "s" * 64, secs: float = 3.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    prov = {"outputs": {"sheet.png": sheet}, "process_seconds": secs}
    if sel_sha:
        prov["selection_sha256"] = sel_sha
    path.write_text(json.dumps({
        "pass": passed, "failures": [] if passed else ["x"], "cycle": {"foot_slide_max": 4.5},
        "frames": [{"off_palette_before": 2}, {"off_palette_before": 3}], "provenance": prov}))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / DATE
    r.mkdir()
    return r


def score(root: Path, review: dict | None = None) -> list[dict]:
    return S.rows(root, review or {})


def test_all_failed_sets_still_get_a_row(root: Path) -> None:
    manifest(root, f"{DATE}/A/samurai/1", exit_code=1, wall_s=120)
    manifest(root, f"{DATE}/A/oathguard/1", exit_code=2, wall_s=300)
    rs = score(root)
    assert sorted(r["character"] for r in rs) == [1, 2]
    for r in rs:
        assert r["attempts"] == 1 and r["failed_attempts"] == 1
        assert r["gen_minutes"] == "" and r["usable"] == "" and r["validator_pass"] == ""
    assert {r["attempt_minutes_total"] for r in rs} == {2.0, 5.0}


def test_failed_attempt_time_is_not_hidden_by_the_retry(root: Path) -> None:
    manifest(root, f"{DATE}/A/samurai/1", exit_code=1, wall_s=600)
    manifest(root, f"{DATE}/A/samurai/1-r1", exit_code=0, wall_s=120, mem=(100.0, 40.0))
    (r,) = score(root)
    assert r["attempts"] == 2 and r["failed_attempts"] == 1
    assert r["gen_minutes"] == 2.0 and r["attempt_minutes_total"] == 12.0
    assert r["peak_mem_gb"] == 60.0
    assert "not assembled" in r["notes"] and r["validator_pass"] == "" and r["processed_sha256"] == ""


def test_selection_decides_gen_minutes(root: Path) -> None:
    j0, j1 = f"{DATE}/A/samurai/1", f"{DATE}/A/samurai/1-r1"
    manifest(root, j0, wall_s=600)
    manifest(root, j1, wall_s=120)
    selection(root / "A/samurai/1/set", [att(j0, 600, selected=False), att(j1, 120)])
    (r,) = score(root)
    assert r["gen_minutes"] == 2.0 and r["attempt_minutes_total"] == 12.0 and r["failed_attempts"] == 0
    assert "not assembled" not in r["notes"] and "not processed" in r["notes"]


def test_missing_wall_time_is_blank_not_zero(root: Path) -> None:
    manifest(root, f"{DATE}/A/samurai/1", wall_s=None)
    (r,) = score(root)
    assert r["gen_minutes"] == "" and r["attempt_minutes_total"] == "" and r["process_seconds"] == ""


@pytest.mark.parametrize("sel_pass", [True, False])
def test_report_comes_from_the_selection_not_from_attempt_dirs(root: Path, sel_pass: bool) -> None:
    """Obsolete reports lying in the attempt dirs (one PASS, one FAIL) never decide the verdict."""
    j0, j1 = f"{DATE}/A/samurai/1", f"{DATE}/A/samurai/1-r1"
    manifest(root, j0, exit_code=1)
    manifest(root, j1)
    report(root / "A/samurai/1/processed-t0/report.json", False, None)
    report(root / "A/samurai/1-r1/processed-t0/report.json", True, None)
    sha = selection(root / "A/samurai/1/set", [att(j0, 60, 1, False), att(j1, 60)])
    report(root / "A/samurai/1/set/processed/report.json", sel_pass, sha, sheet="a" * 64, secs=7.5)
    (r,) = score(root)
    assert r["validator_pass"] == ("yes" if sel_pass else "no")
    assert r["process_seconds"] == 7.5
    assert r["processed_sha256"] == "a" * 64 and r["foot_slide_max_px"] == 4.5 and r["off_palette_before"] == 5


def test_report_not_bound_to_the_selection_is_ignored(root: Path) -> None:
    j = f"{DATE}/A/samurai/1"
    manifest(root, j)
    selection(root / "A/samurai/1/set", [att(j, 60)])
    report(root / "A/samurai/1/set/processed/report.json", True, "0" * 64)       # another selection's report
    (r,) = score(root)
    assert r["validator_pass"] == "" and r["processed_sha256"] == "" and r["process_seconds"] == ""
    assert "report not bound to selection" in r["notes"]
    report(root / "A/samurai/1/set/processed/report.json", True, None)           # pre-provenance report
    assert score(root)[0]["validator_pass"] == ""


def test_not_assembled_ignores_loose_reports(root: Path) -> None:
    manifest(root, f"{DATE}/A/samurai/1")
    report(root / "A/samurai/1/processed-t0/report.json", True, None)
    (r,) = score(root)
    assert r["validator_pass"] == "" and r["processed_sha256"] == "" and "not assembled" in r["notes"]


def test_b_sets_are_per_checkpoint_and_use_the_variant_set_dir(root: Path) -> None:
    for ck in ("ck2750", "ck3000"):
        j = f"{DATE}/B/oathguard/1/{ck}"
        manifest(root, j)
        sha = selection(root / f"B/oathguard/1/{ck}/set", [att(j, 60)], variant=ck)
        report(root / f"B/oathguard/1/{ck}/set/processed/report.json", ck == "ck3000", sha)
    a, b = score(root)
    assert (a["variant"], a["validator_pass"], b["variant"], b["validator_pass"]) == ("ck2750", "no", "ck3000", "yes")


@pytest.mark.parametrize(
    "human,validator_pass,usable",
    [("yes", False, "no"), ("yes", True, "yes"), ("no", True, "no"), ("no", False, "no"), ("", True, ""), ("yes", None, "")],
)
def test_usable_combines_human_and_validator(root: Path, human: str, validator_pass: bool | None, usable: str) -> None:
    j = f"{DATE}/A/samurai/1"
    manifest(root, j)
    sha = selection(root / "A/samurai/1/set", [att(j, 60)])
    if validator_pass is not None:
        report(root / "A/samurai/1/set/processed/report.json", validator_pass, sha, sheet="a" * 64)
    (r,) = score(root, {"reviewer": "t", "sets": {"A/samurai/1": {"usable": human, "artifact_sha256": "a" * 64}}})
    assert r["human_usable"] == human and r["usable"] == usable
    assert r["validator_pass"] == {True: "yes", False: "no", None: ""}[validator_pass]


def test_review_binding(root: Path) -> None:
    j = f"{DATE}/A/samurai/1"
    manifest(root, j)
    sha = selection(root / "A/samurai/1/set", [att(j, 60)])
    report(root / "A/samurai/1/set/processed/report.json", True, sha, sheet="a" * 64)

    def with_review(entry: dict | None) -> dict:
        return score(root, {"sets": {"A/samurai/1": entry} if entry is not None else {}})[0]

    assert with_review(None)["review_binding"] == ""
    bound = with_review({"usable": "yes", "cleanup_minutes": 5, "artifact_sha256": "a" * 64})
    assert bound["review_binding"] == "bound" and bound["usable"] == "yes" and bound["cleanup_minutes"] == 5
    unbound = with_review({"usable": "yes"})
    assert unbound["review_binding"] == "unbound" and unbound["human_usable"] == "yes"
    assert unbound["usable"] == ""    # a verdict not bound to this artifact's hash is not a verdict on it
    stale = with_review({"usable": "yes", "cleanup_minutes": 5, "artifact_sha256": "b" * 64})
    assert stale["review_binding"] == "stale" and stale["human_usable"] == "yes" and stale["cleanup_minutes"] == 5
    assert "review was of a different artifact" in stale["notes"]
    assert stale["usable"] == ""      # a verdict on another artifact is not a verdict on this one


@pytest.mark.parametrize("entry", [
    {"usable": "yes"},                                  # unbound: no artifact hash
    {"usable": "yes", "artifact_sha256": ""},           # unbound: empty hash
    {"usable": "yes", "artifact_sha256": "b" * 64},     # stale: hash of another artifact
])
@pytest.mark.parametrize("bind_report", [True, False])
def test_unbound_or_stale_review_never_makes_usable_yes(root: Path, entry: dict, bind_report: bool) -> None:
    """Regression (Phase 1 senior review): with validator_pass yes, only a review bound to this sheet's hash may
    complete usable=yes. A report not bound to selection.json has no sheet hash, so any hash in the review is stale."""
    j = f"{DATE}/E/relay_sentinel/2/north"
    manifest(root, j)
    sdir = root / "E/relay_sentinel/2/set"
    sha = selection(sdir, [att(j, 60)])
    report(sdir / "processed/report.json", True, sha if bind_report else "0" * 64, sheet="a" * 64)
    (r,) = score(root, {"sets": {"E/relay_sentinel/2": entry}})
    assert r["human_usable"] == "yes" and r["review_binding"] in ("unbound", "stale")
    assert r["usable"] != "yes"


def test_cli_writes_the_columns(root: Path, tmp_path: Path) -> None:
    manifest(root, f"{DATE}/A/samurai/1", exit_code=1)
    out = tmp_path / "scoring.csv"
    S.main([str(root), str(out)])
    got = list(csv.DictReader(out.open()))
    assert list(got[0]) == S.COLS and got[0]["failed_attempts"] == "1" and got[0]["gen_minutes"] == ""
