"""Imported profile templates are confined before the loader follows them."""

import builtins
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import test_records as fixtures
import yaml

from sonoranlab import registry
from sonoranlab.api import ToolError
from sonoranlab.artifacts import sha256_file

setup, job, template = fixtures.setup, fixtures.job, fixtures.template


@pytest.mark.parametrize("scope", ["global", "character"])
def test_import_refuses_template_symlink_before_read(setup, job, monkeypatch, tmp_path_factory, scope):
    ctx, _, _ = setup
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", str(job.parent))
    external = tmp_path_factory.mktemp("outside-read-roots") / "template.yaml"
    external.write_text("directions: [north]\nframe_sets: {2: {}}\n")
    template = job.parent / "poses"
    template.mkdir()
    (template / "template.yaml").symlink_to(external)
    manifest_path = job / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    data = yaml.safe_load(Path(manifest["profile"]["path"]).read_text())
    animations = data["animations"] if scope == "global" else data["characters"]["hero"].setdefault("animations", {})
    animations.setdefault("idle", {})["template"] = str(template)
    profile = job.parent / "untrusted.yaml"
    profile.write_text(yaml.safe_dump(data))
    manifest["profile"] = {"path": str(profile), "sha256": sha256_file(profile)}
    manifest_path.write_text(json.dumps(manifest))
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        assert path.resolve() != external, "template opened before confinement"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(ToolError, match="Symlink escapes") as exc:
        registry.import_job("toy", "hero", str(job), ctx=ctx)
    assert exc.value.error["code"] == "path_refused"


def test_repository_profile_templates_remain_trusted(setup, job, monkeypatch):
    ctx, _, _ = setup
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", str(job))
    profile = ctx.repo / "profiles/toy.yaml"
    data = yaml.safe_load(profile.read_text())
    data["animations"]["idle"]["template"] = "../templates/idle"
    template = ctx.repo / "templates/idle"
    template.mkdir(parents=True)
    (template / "template.yaml").write_text(
        yaml.safe_dump({"directions": data["directions"]["generate"], "frame_sets": {2: {}}})
    )
    profile.write_text(yaml.safe_dump(data))
    manifest_path = job / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["profile"] = {"path": str(profile), "sha256": sha256_file(profile)}
    manifest_path.write_text(json.dumps(manifest))
    assert registry.import_job("toy", "hero", str(job), ctx=ctx)["created"]


def test_historical_profile_with_relative_template_is_imported(setup, job, monkeypatch):
    """Real profiles say `template: ../templates/poses/run`; the `..` is resolved against the profile's directory."""
    ctx, _, _ = setup
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", str(job.parent))
    manifest_path = job / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    data = yaml.safe_load(Path(manifest["profile"]["path"]).read_text())
    data["animations"]["idle"]["template"] = "../templates/idle"
    checkout = job.parent / "historical-checkout"
    (checkout / "profiles").mkdir(parents=True)
    (checkout / "templates/idle").mkdir(parents=True)
    (checkout / "templates/idle/template.yaml").write_text(
        yaml.safe_dump({"directions": data["directions"]["generate"], "frame_sets": {2: {}}})
    )
    profile = checkout / "profiles/toy.yaml"
    profile.write_text(yaml.safe_dump(data))
    manifest["profile"] = {"path": str(profile), "sha256": sha256_file(profile)}
    manifest_path.write_text(json.dumps(manifest))
    result = registry.import_job("toy", "hero", str(job), ctx=ctx)
    assert result["artifact_id"].startswith("import-")


def _repo_with(root, *versions):
    """A repository standing in for this one: one commit per profile version at profiles/toy.yaml; returns the revs."""
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    (root / "profiles").mkdir(parents=True)
    subprocess.run([*git, "init", "-q"], check=True)
    revs = []
    for text in versions:
        (root / "profiles/toy.yaml").write_text(text)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "p"], check=True)
        revs.append(subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout)
    return [rev.strip() for rev in revs]


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _outside_committed_profile(job, monkeypatch, tmp_path_factory, edit=None):
    """The job's profile lives outside the read roots and this checkout (a removed or foreign worktree); this repo's
    git holds the same bytes under the same path suffix."""
    from sonoranlab import repo_inputs

    manifest_path = job / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    data = yaml.safe_load(Path(manifest["profile"]["path"]).read_text())
    if edit:
        edit(data)
    text = yaml.safe_dump(data)
    repo = tmp_path_factory.mktemp("repo")
    (rev,) = _repo_with(repo, text)
    profile = tmp_path_factory.mktemp("outside-read-roots") / "wt/profiles/toy.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text(text)
    manifest["profile"] = {"path": str(profile), "sha256": _sha(text)}
    manifest["git"] = rev
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(repo_inputs, "_REPO", repo)
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", str(job.parent))
    return profile


def test_import_admits_an_outside_profile_only_through_git_and_never_opens_it(
    setup, job, monkeypatch, tmp_path_factory
):
    ctx, _, _ = setup
    profile = _outside_committed_profile(job, monkeypatch, tmp_path_factory)
    for name in ("read_text", "read_bytes", "open"):
        original = getattr(Path, name)

        def guard(path, *args, _original=original, **kwargs):
            assert path.resolve() != profile.resolve(), "outside profile opened"
            return _original(path, *args, **kwargs)

        monkeypatch.setattr(Path, name, guard)
    original_open = builtins.open

    def guarded_open(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)):
            assert Path(path).resolve() != profile.resolve(), "outside profile opened"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    result = registry.import_job("toy", "hero", str(job), ctx=ctx)
    assert result["artifact_id"].startswith("import-")
    review = registry.check_review(result["artifact_id"], ctx=ctx)
    assert review["review_state"] == "none" and not review["production_usable"]  # nothing inherited


def test_recovered_profile_must_name_the_project(setup, job, monkeypatch, tmp_path_factory):
    ctx, _, _ = setup
    _outside_committed_profile(job, monkeypatch, tmp_path_factory, edit=lambda d: d.update(id="other"))
    with pytest.raises(ToolError) as exc:
        registry.import_job("toy", "hero", str(job), ctx=ctx)
    assert exc.value.error["code"] == "invalid_params"


def test_outside_profile_without_a_git_match_is_still_refused(setup, job, monkeypatch, tmp_path_factory):
    from sonoranlab import repo_inputs

    ctx, _, _ = setup
    _outside_committed_profile(job, monkeypatch, tmp_path_factory)
    monkeypatch.setattr(repo_inputs, "_REPO", job.parent)  # no such blob in this repository
    with pytest.raises(ToolError) as exc:
        registry.import_job("toy", "hero", str(job), ctx=ctx)
    assert exc.value.error["code"] == "path_refused"


def _animate_import(job, monkeypatch, tmp_path_factory, *, versions, final, animate_binding, repo_rev=None):
    """An animate wrapper whose final job and animate.json bind removed-worktree profiles (never on disk)."""
    from sonoranlab import repo_inputs

    base = yaml.safe_dump(
        yaml.safe_load(Path(json.loads((job / "manifest.json").read_text())["profile"]["path"]).read_text())
    )
    texts = [base + suffix for suffix in versions]
    repo = tmp_path_factory.mktemp("repo")
    revs = _repo_with(repo, *texts)
    gone = tmp_path_factory.mktemp("removed")
    outer = job.parent / "animate"
    current = shutil.copytree(job, outer / "sweep/final/job")
    manifest = json.loads((current / "manifest.json").read_text())
    manifest["profile"] = {"path": str(gone / "final-wt/profiles/toy.yaml"), "sha256": _sha(texts[final])}
    manifest["git"] = revs[final]
    (current / "manifest.json").write_text(json.dumps(manifest))
    path, version = animate_binding
    inputs = {
        "profile": {"path": str(gone / path), "sha256": _sha(texts[version])},
        "character": "hero",
        "anim": "idle",
    }
    if repo_rev is not None:
        inputs["repo_rev"] = revs[repo_rev]
    animate = {"schema": "sonoranlab.animate/1", "outcome": "pass", "exit": 0, "inputs": inputs}
    animate["verdict"] = {"pass": True, "job": str(current)}
    (outer / "animate.json").write_text(json.dumps(animate))
    monkeypatch.setattr(repo_inputs, "_REPO", repo)
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", str(job.parent))
    return outer


def test_differing_animate_binding_does_not_borrow_the_final_jobs_commit(setup, job, monkeypatch, tmp_path_factory):
    ctx, _, _ = setup
    # Same bytes, another path: not the final job's binding, and animate.json records no commit of its own.
    outer = _animate_import(
        job, monkeypatch, tmp_path_factory, versions=[""], final=0, animate_binding=("anim-wt/profiles/toy.yaml", 0)
    )
    with pytest.raises(ToolError) as exc:
        registry.import_job("toy", "hero", str(outer), ctx=ctx)
    assert exc.value.error["code"] == "path_refused"


def test_differing_animate_binding_recovers_at_its_own_commit(setup, job, monkeypatch, tmp_path_factory):
    ctx, _, _ = setup
    outer = _animate_import(
        job,
        monkeypatch,
        tmp_path_factory,
        versions=["", "# revised\n"],
        final=1,
        animate_binding=("anim-wt/profiles/toy.yaml", 0),
        repo_rev=0,
    )
    assert registry.import_job("toy", "hero", str(outer), ctx=ctx)["artifact_id"].startswith("import-")


def test_identical_animate_binding_uses_the_final_jobs_commit(setup, job, monkeypatch, tmp_path_factory):
    ctx, _, _ = setup
    outer = _animate_import(
        job, monkeypatch, tmp_path_factory, versions=[""], final=0, animate_binding=("final-wt/profiles/toy.yaml", 0)
    )
    assert registry.import_job("toy", "hero", str(outer), ctx=ctx)["artifact_id"].startswith("import-")


def _edited_repo_profile(setup, job, monkeypatch, edit):
    """The job ran against the repository profile (with a second character) at a recorded commit; the profile has
    since been edited in place by `edit`."""
    from sonoranlab import repo_inputs

    ctx, _, _ = setup
    profile = ctx.repo / "profiles/toy.yaml"
    doc = yaml.safe_load(profile.read_text())
    doc["characters"]["villain"] = dict(doc["characters"]["hero"])
    (rev,) = _repo_with(ctx.repo / "git", yaml.safe_dump(doc))  # this repository's history; profiles/ has no git
    profile.write_text(yaml.safe_dump(doc))
    manifest_path = job / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["profile"] = {"path": str(profile), "sha256": sha256_file(profile)}
    manifest["git"] = rev
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(repo_inputs, "_REPO", ctx.repo / "git")
    edit(doc)
    profile.write_text(yaml.safe_dump(doc))


def test_import_admits_a_profile_changed_only_in_another_characters_row(setup, job, monkeypatch):
    ctx, _, _ = setup
    _edited_repo_profile(
        setup, job, monkeypatch, lambda d: d["characters"]["villain"].update(max_attempts_per_animation=6)
    )
    assert registry.import_job("toy", "hero", str(job), ctx=ctx)["artifact_id"].startswith("import-")


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d["characters"]["hero"].update(cell=d["characters"]["hero"]["cell"] + 1),
        lambda d: d["autonomy"].update(max_seeds_per_attempt=2),
    ],
)
def test_import_refuses_an_own_row_or_shared_setting_change(setup, job, monkeypatch, edit):
    ctx, _, _ = setup
    _edited_repo_profile(setup, job, monkeypatch, edit)
    with pytest.raises(ToolError) as exc:
        registry.import_job("toy", "hero", str(job), ctx=ctx)
    assert exc.value.error["code"] == "invalid_params" and "requested project" in exc.value.error["message"]


def test_import_relabelled_report_gets_no_row_equivalence(setup, job, monkeypatch):
    ctx, _, _ = setup
    _edited_repo_profile(setup, job, monkeypatch, lambda d: d["characters"]["villain"].update(cell=99))
    report = json.loads((job / "report.json").read_text())
    report["character"] = "villain"  # report and manifest disagree: the equivalence has no row to trust
    (job / "report.json").write_text(json.dumps(report))
    manifest = json.loads((job / "manifest.json").read_text())
    manifest["outputs"]["report.json"] = sha256_file(job / "report.json")
    (job / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ToolError, match="requested project"):
        registry.import_job("toy", "hero", str(job), ctx=ctx)
