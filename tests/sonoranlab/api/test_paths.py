import os
from pathlib import Path

import pytest

from sonoranlab.api import ToolError, paths


@pytest.fixture
def roots(tmp_path, monkeypatch):
    read, state, outside, game = (tmp_path / name for name in ("read", "state", "outside", "read/game"))
    for path in (read, state, outside, game):
        path.mkdir()
        (path / "input.txt").write_text("test")
    monkeypatch.setenv("SONORANLAB_STATE", str(state))
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", str(read))
    monkeypatch.setattr(paths, "_game_roots", lambda: (game,))
    return read, state, outside, game


def refused(fn, path):
    with pytest.raises(ToolError) as exc:
        fn(path)
    assert exc.value.error["code"] == "path_refused"


def test_defaults_and_no_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SONORANLAB_STATE", raising=False)
    monkeypatch.delenv("SONORANLAB_READ_ROOTS", raising=False)
    assert paths.state_root() == tmp_path / "sonoranlab-work/mcp"
    assert paths.read_roots() == (tmp_path / "sonoranlab-work",)
    assert list(tmp_path.iterdir()) == []


def test_regular_absolute_and_relative_inputs(roots, monkeypatch):
    read, state, outside, game = roots
    assert paths.resolve_input(read / "input.txt") == read / "input.txt"
    assert paths.resolve_input(state / "input.txt") == state / "input.txt"
    monkeypatch.chdir(read)
    assert paths.resolve_input("input.txt") == read / "input.txt"
    refused(paths.resolve_input, outside / "input.txt")
    refused(paths.resolve_input, game / "input.txt")
    refused(paths.resolve_input, read)
    with pytest.raises(FileNotFoundError):
        paths.resolve_input(read / "absent")


@pytest.mark.parametrize("absolute", [False, True])
def test_traversal_rejected_even_if_resolves_inside(roots, monkeypatch, absolute):
    read, state, _outside, _game = roots
    monkeypatch.chdir(read)
    candidate = Path("game/../input.txt")
    refused(paths.resolve_input, read / candidate if absolute else candidate)
    refused(paths.resolve_output, state / "child/../new")
    refused(paths.resolve_output, "../state/new")


def test_input_symlinks(roots):
    read, state, outside, game = roots
    for name, target in (("outside", outside), ("game-link", game), ("other-root", state)):
        (read / name).symlink_to(target, target_is_directory=True)
        refused(paths.resolve_input, read / name / "input.txt")
    (read / "internal").symlink_to(read / "input.txt")
    assert paths.resolve_input(read / "internal") == read / "input.txt"
    (read / "dangling").symlink_to(outside / "absent")
    refused(paths.resolve_input, read / "dangling")
    (read / "loop").symlink_to(read / "loop")
    refused(paths.resolve_input, read / "loop")


def test_output_confinement_and_exclusive_leaf(roots):
    read, state, outside, game = roots
    assert paths.resolve_output("new") == state / "new"
    assert paths.resolve_output(state / "new") == state / "new"
    assert not (state / "new").exists()
    refused(paths.resolve_output, outside / "new")
    refused(paths.resolve_output, read / "new")
    refused(paths.resolve_output, game / "new")
    with pytest.raises(FileExistsError):
        paths.resolve_output("input.txt")
    (state / "existing").mkdir()
    with pytest.raises(FileExistsError):
        paths.resolve_output("existing")
    with pytest.raises(FileNotFoundError):
        paths.resolve_output("missing-parent/new")
    refused(paths.resolve_output, "input.txt/new")


def test_output_symlink_ancestors_and_leaves(roots):
    _read, state, outside, game = roots
    for name, target in (("out", outside), ("game", game)):
        (state / name).symlink_to(target, target_is_directory=True)
        refused(paths.resolve_output, f"{name}/new")
    for name, target in (("file", outside / "input.txt"), ("dangling", outside / "absent")):
        (state / name).symlink_to(target)
        refused(paths.resolve_output, name)
    (state / "inner").mkdir()
    (state / "inside").symlink_to(state / "inner", target_is_directory=True)
    assert paths.resolve_output("inside/new") == state / "inner/new"


def test_game_inside_state_is_protected(roots, monkeypatch):
    _read, state, _outside, _game = roots
    monkeypatch.setattr(paths, "_game_roots", lambda: (state / "protected",))
    (state / "protected").mkdir()
    (state / "protected/input").write_text("private")
    refused(paths.resolve_input, state / "protected/input")
    refused(paths.resolve_output, "protected/new")
    # Refuse the game directory itself even if it does not exist yet.
    (state / "protected/input").unlink()
    (state / "protected").rmdir()
    refused(paths.resolve_output, "protected")


def test_root_prefix_and_non_regular_inputs(roots, monkeypatch):
    read, _state, outside, _game = roots
    sibling = read.with_name(read.name + "-evil")
    sibling.mkdir()
    (sibling / "input").write_text("outside")
    refused(paths.resolve_input, sibling / "input")
    os.mkfifo(read / "fifo")
    refused(paths.resolve_input, read / "fifo")
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", os.pathsep.join((str(read), str(outside))))
    assert paths.resolve_input(outside / "input.txt") == outside / "input.txt"
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", "/dev")
    refused(paths.resolve_input, "/dev/null")


def test_empty_read_roots_never_grant_cwd(roots, monkeypatch):
    read, state, _outside, _game = roots
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", os.pathsep)
    assert paths.read_roots() == ()
    refused(paths.resolve_input, read / "input.txt")
    assert paths.resolve_input(state / "input.txt") == state / "input.txt"


def test_missing_targets_still_refuse_escapes_and_game_reads(roots):
    read, state, outside, game = roots
    (read / "missing-tree").symlink_to(outside / "not-created", target_is_directory=True)
    refused(paths.resolve_input, read / "missing-tree/file")
    refused(paths.resolve_input, game / "missing-file")
    (state / "missing-tree").symlink_to(outside / "not-created", target_is_directory=True)
    refused(paths.resolve_output, "missing-tree/file")


@pytest.mark.parametrize("path", ["", "bad\x00path"])
def test_invalid_path_strings(roots, path):
    refused(paths.resolve_input, path)
    refused(paths.resolve_output, path)


def test_protected_game_is_read_from_profile(tmp_path, monkeypatch):
    import yaml

    from sonoranlab.api import Context
    from sonoranlab.api.registry import REPO_ROOT

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    game = tmp_path / "game"
    game.mkdir()
    (game / "private.txt").write_text("private")
    profile = yaml.safe_load((REPO_ROOT / "profiles/twos-are-up.yaml").read_text())
    profile["export"]["godot_project"] = str(game)
    profile["animations"]["run"]["template"] = str(REPO_ROOT / "templates/poses/run")
    (profiles / "test.yaml").write_text(yaml.safe_dump(profile))
    monkeypatch.setenv("SONORANLAB_STATE", str(tmp_path))
    monkeypatch.setenv("SONORANLAB_READ_ROOTS", str(tmp_path))
    monkeypatch.setattr(paths, "Context", lambda: Context(repo=tmp_path))
    assert paths._game_roots() == (game,)
    refused(paths.resolve_input, game / "private.txt")
    refused(paths.resolve_output, game / "new")
