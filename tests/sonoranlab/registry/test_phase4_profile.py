"""Imported profile templates are confined before the loader follows them."""

import json
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


def _outside_committed_profile(job, monkeypatch, tmp_path_factory, edit=None):
    """The job's profile lives outside the read roots (a removed or foreign worktree) and in this repo's git."""
    import hashlib
    import subprocess

    from sonoranlab import repo_inputs

    manifest_path = job / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    repo = tmp_path_factory.mktemp("outside-read-roots") / "wt"
    (repo / "profiles").mkdir(parents=True)
    data = yaml.safe_load(Path(manifest["profile"]["path"]).read_text())
    if edit:
        edit(data)
    profile = repo / "profiles/toy.yaml"
    profile.write_text(yaml.safe_dump(data))
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "p"]):
        subprocess.run([*git, *args], check=True)
    rev = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    manifest["profile"] = {"path": str(profile), "sha256": hashlib.sha256(profile.read_bytes()).hexdigest()}
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
