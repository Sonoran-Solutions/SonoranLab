"""Confinement checks are read-only, including on every refusal path."""

from pathlib import Path

import pytest

from sonoranlab.artifacts import _REPO
from sonoranlab.repair import Refused
from sonoranlab.repair.confine import agent_root, check_destination, git_root, repair_root


def snapshot(root):
    return {
        p.relative_to(root): (p.lstat().st_mode, p.read_bytes() if p.is_file() and not p.is_symlink() else None)
        for p in root.rglob("*")
    }


@pytest.fixture
def refuse(tmp_path):
    def check(code, call):
        before = snapshot(tmp_path)
        with pytest.raises(Refused, match=f"^{code}:"):
            call()
        assert snapshot(tmp_path) == before

    return check


@pytest.mark.parametrize(
    "get_root,env,leaf",
    [
        (repair_root, "SONORANLAB_REPAIR_ROOT", "repairs"),
        (agent_root, "SONORANLAB_AGENT_ROOT", "agents"),
    ],
)
def test_explicit_roots(tmp_path, monkeypatch, get_root, env, leaf, refuse):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(env, raising=False)
    before = snapshot(tmp_path)
    assert get_root() == tmp_path / "sonoranlab-work" / leaf
    monkeypatch.setenv(env, "~/custom")
    assert get_root() == tmp_path / "custom"
    assert snapshot(tmp_path) == before
    monkeypatch.setenv(env, "relative/root")
    refuse("root_not_absolute", get_root)


def test_valid_destination_creates_nothing(tmp_path, monkeypatch):
    root = tmp_path / "missing/root"
    before = snapshot(tmp_path)
    assert check_destination(root / "child/../repair", root=root) == root / "repair"
    monkeypatch.chdir(tmp_path)
    assert check_destination(Path("missing/root/repair"), root=root) == root / "repair"
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("relative", ["root", "elsewhere/new", "root-sibling/new", "root/../new"])
def test_must_be_strictly_inside(tmp_path, relative, refuse):
    refuse("outside_root", lambda: check_destination(tmp_path / relative, root=tmp_path / "root"))


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_existing_destination(tmp_path, kind, refuse):
    path = tmp_path / "repair"
    if kind == "file":
        path.write_text("keep")
    else:
        path.mkdir()
    refuse("destination_exists", lambda: check_destination(path, root=tmp_path))
    before = snapshot(tmp_path)
    assert check_destination(path, root=tmp_path, must_not_exist=False) == path
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("dangling", [False, True])
@pytest.mark.parametrize("suffix", ["", "child/new", "../new"])
def test_symlink_before_resolving(tmp_path, dangling, suffix, refuse):
    target = tmp_path / "target"
    if not dangling:
        target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    path = link / suffix
    refuse("symlink_path", lambda: check_destination(path, root=tmp_path))


@pytest.mark.parametrize("entry", ["directory", "file"])
def test_git_working_tree(tmp_path, entry, refuse):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    if entry == "directory":
        (checkout / ".git").mkdir()
        (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    else:
        (checkout / ".git").write_text("gitdir: /somewhere/else")
    path = checkout / "nested/new/repair"
    assert git_root(path) == checkout
    refuse("git_worktree", lambda: check_destination(path, root=tmp_path))
    refuse("git_worktree", lambda: check_destination(path, root=tmp_path, must_not_exist=False))


def test_real_sonoranlab_repository(refuse):
    path = _REPO / "private-repair-must-not-be-created" / "out"
    before = snapshot(_REPO)
    refuse("repository_path", lambda: check_destination(path, root=_REPO.parent))
    refuse("forbidden_path", lambda: check_destination(_REPO.parent, root=_REPO.parent.parent))
    assert snapshot(_REPO) == before


@pytest.mark.parametrize("relation", ["inside", "equal", "contains"])
def test_forbidden_overlap(tmp_path, relation, refuse):
    area = tmp_path / "private/job"
    path = {"inside": area / "new", "equal": area, "contains": area.parent}[relation]
    refuse("forbidden_path", lambda: check_destination(path, root=tmp_path, forbidden=[area]))


@pytest.mark.parametrize(
    "marker",
    [
        "manifest.json",
        "review.json",
        "report.json",
        "repair.json",
        ".sonoranlab-staging",
        "mapping.json",
        "state.json",
        "repair-manifest.json",
        "authorization.json",
    ],
)
@pytest.mark.parametrize("at_root", [False, True])
def test_nested_job_or_repair(tmp_path, marker, at_root, refuse):
    job = tmp_path if at_root else tmp_path / "older-job"
    job.mkdir(exist_ok=True)
    (job / marker).write_text("{}")
    refuse("nested_workspace", lambda: check_destination(job / "nested/new/repair", root=tmp_path))


@pytest.mark.parametrize(
    "get_root,env",
    [
        (repair_root, "SONORANLAB_REPAIR_ROOT"),
        (agent_root, "SONORANLAB_AGENT_ROOT"),
    ],
)
@pytest.mark.parametrize("area", ["symlink", "git", "repository", "job"])
def test_unsafe_root(tmp_path, monkeypatch, get_root, env, area, refuse):
    root = tmp_path / "root"
    code = {
        "symlink": "symlink_path",
        "git": "git_worktree",
        "repository": "repository_path",
        "job": "nested_workspace",
    }[area]
    if area == "symlink":
        root.symlink_to(tmp_path / "missing", target_is_directory=True)
    elif area == "repository":
        root = _REPO / "missing-staging-root"
    else:
        root.mkdir()
        (root / (".git" if area == "git" else "repair.json")).write_text(
            "gitdir: /elsewhere\n" if area == "git" else "{}"
        )
    monkeypatch.setenv(env, str(root / "nested"))
    refuse(code, get_root)
    refuse(code, lambda: check_destination(root / "nested/new", root=root / "nested"))


def test_root_inside_forbidden(tmp_path, refuse):
    root = tmp_path / "game/repairs"
    refuse("forbidden_path", lambda: check_destination(root / "new", root=root, forbidden=[tmp_path / "game"]))


def test_relative_root_argument(refuse):
    refuse("root_not_absolute", lambda: check_destination(Path("new"), root=Path("relative")))


def test_empty_git_dir_is_not_a_checkout(tmp_path):
    """Sandboxes leave empty `.git` mount points (e.g. /tmp/.git); only real markers count."""
    (tmp_path / "stray" / ".git").mkdir(parents=True)
    assert git_root(tmp_path / "stray" / "x") is None
    (tmp_path / "stray" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    assert git_root(tmp_path / "stray" / "x") == tmp_path / "stray"


@pytest.mark.parametrize("variable", ["GIT_DIR", "GIT_WORK_TREE"])
def test_git_environment_areas(tmp_path, monkeypatch, variable, refuse):
    area = tmp_path / "environment-area"
    area.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(area, target_is_directory=True)
    monkeypatch.setenv(variable, str(alias))
    refuse("git_worktree", lambda: check_destination(area / "new", root=tmp_path))


def test_real_git_environment_checkout_and_bare_repo(tmp_path, monkeypatch, refuse):
    import shutil
    import subprocess

    if not shutil.which("git"):
        pytest.skip("git unavailable")
    bare = tmp_path / "external.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    refuse("git_worktree", lambda: check_destination(bare / "new", root=tmp_path))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setenv("GIT_DIR", str(bare))
    monkeypatch.setenv("GIT_WORK_TREE", str(checkout))
    refuse("git_worktree", lambda: check_destination(checkout / "new", root=tmp_path))


def test_git_command_uses_nearest_directory_and_inherited_env(tmp_path, monkeypatch, refuse):
    import subprocess

    from sonoranlab.repair import confine

    commands = []

    def git(args, **kwargs):
        commands.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "true\nfalse\n/worktree\n/git-dir\n", "")

    monkeypatch.setattr(confine.subprocess, "run", git)
    refuse("git_worktree", lambda: check_destination(tmp_path / "missing/new", root=tmp_path))
    assert commands[0][0] == [
        "git",
        "-C",
        str(tmp_path),
        "rev-parse",
        "--is-inside-work-tree",
        "--is-inside-git-dir",
        "--show-toplevel",
        "--absolute-git-dir",
    ]
    assert commands[0][1]["timeout"] == 5 and "env" not in commands[0][1]


@pytest.mark.parametrize("failure", ["missing", "timeout"])
def test_git_unavailable_falls_back_to_positive_markers(tmp_path, monkeypatch, failure):
    import subprocess

    from sonoranlab.repair import confine

    def unavailable(*args, **kwargs):
        if failure == "missing":
            raise FileNotFoundError("no git")
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr(confine.subprocess, "run", unavailable)
    assert check_destination(tmp_path / "safe", root=tmp_path) == tmp_path / "safe"
    (tmp_path / ".git").write_text("gitdir: /elsewhere")
    with pytest.raises(Refused, match="git_worktree"):
        check_destination(tmp_path / "unsafe", root=tmp_path)
