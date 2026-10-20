"""Read-only destination checks shared by repair and agent staging commands.

Refused messages start with a stable refusal code, followed by path-specific detail.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from sonoranlab import artifacts
from sonoranlab.repair import Refused

_MARKERS = ("manifest.json", "review.json", "report.json", "repair.json")


def _checked_path(path: Path) -> Path:
    path = Path(path).expanduser().absolute()
    # Keep '..' until after checking: link/../out still traverses a symlink.
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise Refused(f"symlink_path: {ancestor}: symlinked path refused")
    return path.resolve()


def _is_git_entry(entry: Path) -> bool:
    """A real checkout marker: a `.git` dir with HEAD, a `gitdir:` file (worktree), or any symlink (conservative).

    An empty `.git` directory is not a checkout (sandboxes create such mount points, e.g. /tmp/.git).
    """
    if entry.is_symlink():
        return True
    if entry.is_dir():
        return (entry / "HEAD").is_file()
    if entry.is_file():
        try:
            with entry.open("rb") as f:
                return f.read(8) == b"gitdir: "
        except OSError:
            return True
    return False


def git_root(path: Path) -> Path | None:
    """Find the enclosing Git checkout or worktree without running Git."""
    path = Path(path).resolve()
    for ancestor in (path, *path.parents):
        if _is_git_entry(ancestor / ".git"):
            return ancestor
    return None


def _check_area(path: Path, forbidden: Iterable[Path] = ()) -> None:
    if path.is_relative_to(artifacts._REPO.resolve()):
        raise Refused(f"repository_path: {path} is inside the SonoranLab repository")
    for area in forbidden:
        if path.is_relative_to(area):
            raise Refused(f"forbidden_path: {path} is inside forbidden area {area}")
    if checkout := git_root(path):
        raise Refused(f"git_worktree: {path} is inside Git working tree {checkout}")
    for ancestor in (path, *path.parents):
        for marker in _MARKERS:
            entry = ancestor / marker
            if entry.exists() or entry.is_symlink():
                raise Refused(f"nested_workspace: {path} is inside an existing job/repair ({entry})")


def _checked_root(root: Path, forbidden: Iterable[Path] = ()) -> Path:
    root = Path(root).expanduser()
    if not root.is_absolute():
        raise Refused(f"root_not_absolute: {root}: staging root must be absolute")
    root = _checked_path(root)
    _check_area(root, forbidden)
    return root


def repair_root() -> Path:
    """Validated SONORANLAB_REPAIR_ROOT, defaulting to ~/sonoranlab-work/repairs; creates nothing."""
    return _checked_root(Path(os.environ.get("SONORANLAB_REPAIR_ROOT", "~/sonoranlab-work/repairs")))


def agent_root() -> Path:
    """Validated SONORANLAB_AGENT_ROOT, defaulting to ~/sonoranlab-work/agents; creates nothing."""
    return _checked_root(Path(os.environ.get("SONORANLAB_AGENT_ROOT", "~/sonoranlab-work/agents")))


def check_destination(path: Path, *, root: Path, forbidden: Iterable[Path] = (), must_not_exist: bool = True) -> Path:
    """Return a canonical destination strictly inside a safe root, without creating anything.

    Neither the destination nor a forbidden path (including SonoranLab) may contain
    the other. Existing destinations are allowed only when must_not_exist is false;
    all other checks still apply.
    """
    forbidden = tuple(Path(area).expanduser().resolve() for area in forbidden)
    root = _checked_root(root, forbidden)
    path = _checked_path(path)
    if path == root or not path.is_relative_to(root):
        raise Refused(f"outside_root: {path} must be strictly inside staging root {root}")
    _check_area(path, forbidden)
    for area in (artifacts._REPO.resolve(), *forbidden):
        if area.is_relative_to(path):
            raise Refused(f"forbidden_path: {path} would contain forbidden area {area}")
    if must_not_exist and path.exists():
        raise Refused(f"destination_exists: {path}: exists; staging directories are never reused")
    return path
