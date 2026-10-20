"""Test read-only repair admission before mutating commands."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest
from test_interchange import fixture  # noqa: F401

from sonoranlab import artifacts
from sonoranlab.cli import main
from sonoranlab.repair import Refused
from sonoranlab.repair import workspace as ws
from sonoranlab.repair.confine import admit_repair

MUTATING_VERBS = ("export", "authorize", "abort", "session start", "finalize", "relay")
INVALID_CASES = ("a", "b", "c", "d", "e", "f", "g", "h", "i")


def snapshot(root: Path) -> dict:
    if not root.exists():
        return {}
    if not root.is_dir() or root.is_symlink():
        return {
            root: (
                root.lstat().st_mode,
                root.read_bytes() if root.is_file() and not root.is_symlink() else None,
            )
        }
    return {
        p.relative_to(root): (
            p.lstat().st_mode,
            p.read_bytes() if p.is_file() and not p.is_symlink() else None,
        )
        for p in root.rglob("*")
    }


def make_invalid_target(case: str, tmp_path: Path, fixture_val: tuple) -> tuple[Path, Path]:
    """Return (target_path, root_to_snapshot)."""
    job, profile, _ = fixture_val
    repairs_root = tmp_path / "repairs"
    repairs_root.mkdir(parents=True, exist_ok=True)

    if case == "a":
        # a fake game checkout (.git/HEAD present) inside the repair root
        target = repairs_root / "fake_checkout"
        target.mkdir(parents=True, exist_ok=True)
        (target / ".git").mkdir(parents=True, exist_ok=True)
        (target / ".git/HEAD").write_text("ref: refs/heads/main\n")
        (target / "dummy.txt").write_text("dummy")
        return target, target

    if case == "b":
        # a directory outside the repair root
        target = tmp_path / "outside_dir"
        target.mkdir(parents=True, exist_ok=True)
        (target / "sample.txt").write_text("sample")
        return target, target

    # Prepare a valid repair inside repairs_root for cases c-g
    valid_base = repairs_root / f"valid_{case}"
    ws.prepare(job, profile, "hero", "idle", valid_base, converter=ws.FakeConverter())

    if case == "c":
        # a byte-for-byte COPY of a valid repair placed outside the root
        target = tmp_path / "outside_valid_copy"
        shutil.copytree(valid_base, target)
        return target, target

    if case == "d":
        # a valid-looking repair without .sonoranlab-staging
        target = repairs_root / "repair_no_staging"
        shutil.copytree(valid_base, target)
        (target / ".sonoranlab-staging").unlink()
        return target, target

    if case == "e":
        # a marker whose id differs from repair.json
        target = repairs_root / "repair_marker_id_differs"
        shutil.copytree(valid_base, target)
        (target / ".sonoranlab-staging").write_text(
            json.dumps({"kind": "repair", "id": "mismatched-staging-id"}) + "\n"
        )
        return target, target

    if case == "f":
        # a marker with kind other than "repair"
        target = repairs_root / "repair_wrong_kind"
        shutil.copytree(valid_base, target)
        repair_id = json.loads((target / "repair.json").read_text())["repair_id"]
        (target / ".sonoranlab-staging").write_text(json.dumps({"kind": "mailbox", "id": repair_id}) + "\n")
        return target, target

    if case == "g":
        # a symlinked path component
        target = repairs_root / "symlinked_repair"
        target.symlink_to(valid_base, target_is_directory=True)
        return target, valid_base

    if case == "h":
        # a directory nested inside an existing job (raw/ + processed/ ancestor)
        job_dir = repairs_root / "existing_job"
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "raw").mkdir(parents=True, exist_ok=True)
        (job_dir / "processed").mkdir(parents=True, exist_ok=True)
        target = job_dir / "nested_repair"
        target.mkdir(parents=True, exist_ok=True)
        (target / "nested_file.txt").write_text("nested")
        return target, job_dir

    if case == "i":
        # a path inside the SonoranLab repo (artifacts._REPO)
        target = artifacts._REPO / "unadmitted_repair_test_path"
        return target, artifacts._REPO

    raise ValueError(f"Unknown invalid case: {case}")


def invoke_mutating_verb(verb: str, target: Path, tmp_path: Path) -> tuple[int, Path | None, Path | None]:
    """Execute CLI command and return (exit_code, out_dir_if_any, agent_dir_if_any)."""
    out_dir = agent_dir = None
    if verb == "export":
        code = main(["repair", "export", str(target)])
    elif verb == "authorize":
        spec_file = tmp_path / "dummy_spec.json"
        if not spec_file.exists():
            spec_file.write_text("{}")
        code = main(["repair", "authorize", str(target), "--spec", str(spec_file)])
    elif verb == "abort":
        code = main(["repair", "abort", str(target)])
    elif verb == "session start":
        code = main(["repair", "session", "start", str(target)])
    elif verb == "finalize":
        out_dir = tmp_path / "repairs" / f"out_child_{uuid.uuid4().hex[:8]}"
        code = main(["repair", "finalize", str(target), "--out", str(out_dir)])
    elif verb == "relay":
        agent_dir = tmp_path / "agents" / f"agent_mailbox_{uuid.uuid4().hex[:8]}"
        code = main(["repair", "relay", str(target), "--agent-dir", str(agent_dir)])
    else:
        raise ValueError(f"Unknown verb: {verb}")
    return code, out_dir, agent_dir


@pytest.mark.parametrize("verb", MUTATING_VERBS)
@pytest.mark.parametrize("case", INVALID_CASES)
def test_mutating_verbs_refuse_invalid_repair_unmodified(
    fixture,  # noqa: F811
    tmp_path,
    monkeypatch,
    capsys,
    verb,
    case,
):
    repairs_root = tmp_path / "repairs"
    repairs_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SONORANLAB_REPAIR_ROOT", str(repairs_root))
    monkeypatch.setenv("SONORANLAB_AGENT_ROOT", str(tmp_path / "agents"))

    target, snap_root = make_invalid_target(case, tmp_path, fixture)
    had_session = (target / "session").exists()
    before_snap = snapshot(snap_root)

    code, out_dir, agent_dir = invoke_mutating_verb(verb, target, tmp_path)
    output = capsys.readouterr().out

    assert code != 0, f"Expected non-zero exit for verb {verb} on case {case}, got {code}"
    assert snapshot(snap_root) == before_snap, f"Target tree was modified for verb {verb} on case {case}"
    if not had_session:
        assert not (target / "session").exists(), f"session/ appeared for verb {verb} on case {case}"
    assert not (target / "session/lease.lock").exists(), f"lease.lock appeared for verb {verb} on case {case}"

    if out_dir is not None:
        assert not out_dir.exists(), f"finalize created destination {out_dir} on invalid repair"
    if agent_dir is not None:
        assert not agent_dir.exists(), f"relay created agent_dir {agent_dir} on invalid repair"

    # Also verify refusal message format
    if output.strip():
        try:
            parsed = json.loads(output)
            assert parsed.get("message", "").startswith("repair_not_admitted:")
        except json.JSONDecodeError:
            pass


@pytest.mark.parametrize("case", INVALID_CASES)
def test_admit_repair_directly_raises_refused_with_prefix(fixture, tmp_path, monkeypatch, case):  # noqa: F811
    repairs_root = tmp_path / "repairs"
    repairs_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SONORANLAB_REPAIR_ROOT", str(repairs_root))

    target, snap_root = make_invalid_target(case, tmp_path, fixture)
    before_snap = snapshot(snap_root)

    with pytest.raises(Refused) as excinfo:
        admit_repair(target)

    assert str(excinfo.value).startswith("repair_not_admitted:")
    assert snapshot(snap_root) == before_snap


def test_positive_genuine_repair_admitted_and_export_works(fixture, tmp_path, monkeypatch):  # noqa: F811
    repairs_root = tmp_path / "repairs"
    repairs_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SONORANLAB_REPAIR_ROOT", str(repairs_root))

    job, profile, _ = fixture
    repair_path = repairs_root / "genuine_repair"
    ws.prepare(job, profile, "hero", "idle", repair_path, converter=ws.FakeConverter())
    monkeypatch.setattr(ws, "AsepriteConverter", ws.FakeConverter)

    admitted = admit_repair(repair_path)
    assert admitted == repair_path.resolve()

    exit_code = main(["repair", "export", str(repair_path)])
    assert exit_code == 0
    assert (repair_path / "workspace/export").is_dir()
    assert (repair_path / "workspace/export/export.json").is_file()
