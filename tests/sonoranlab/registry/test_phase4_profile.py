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
