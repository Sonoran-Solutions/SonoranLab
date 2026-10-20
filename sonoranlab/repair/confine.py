"""Destination checks and fd-relative staging reservations.

Destination checks and fd-based reservation defend against mistaken or hostile
paths and symlinks present at check time. They do not defend against a concurrent
same-UID process renaming or swapping ancestor directories during the command;
that process can write to those locations directly anyway. Final verification
is best-effort detection of such interference, not a guarantee. Mailbox I/O uses
directory fds throughout, with queue identity checks at relay boundaries.

ponytail: For isolation from concurrent ancestor swaps, run prepare/finalize inside
a mount namespace (bubblewrap) with only the staging root bound writable, and
the game checkout and repository not mounted.

Refused messages start with a stable refusal code, followed by path-specific detail.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from sonoranlab import artifacts
from sonoranlab.profile import load_profile
from sonoranlab.repair import Refused

_MARKERS = (
    "manifest.json",
    "review.json",
    "report.json",
    "repair.json",
    ".sonoranlab-staging",
    "mapping.json",
    "state.json",
    "repair-manifest.json",
    "authorization.json",
)
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


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
    """Find Git areas using conservative markers, environment, and Git itself."""
    path = Path(path).resolve()
    for variable in ("GIT_DIR", "GIT_WORK_TREE"):
        if os.environ.get(variable):
            area = Path(os.environ[variable]).expanduser().resolve()
            if path.is_relative_to(area):
                return area
    for ancestor in (path, *path.parents):
        if _is_git_entry(ancestor / ".git") or (
            (ancestor / "HEAD").is_file() and (ancestor / "objects").is_dir() and (ancestor / "refs").is_dir()
        ):
            return ancestor
    existing = path
    while not existing.is_dir() and existing != existing.parent:
        existing = existing.parent
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(existing),
                "rev-parse",
                "--is-inside-work-tree",
                "--is-inside-git-dir",
                "--show-toplevel",
                "--absolute-git-dir",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        # Even a partial positive answer is grounds for refusal.
        if "true" in getattr(result, "stdout", "").splitlines()[:2]:
            return existing
    except (OSError, subprocess.SubprocessError):
        pass  # Positive markers above never depend on being able to execute Git.
    return None


def _check_area(
    path: Path,
    forbidden: Iterable[Path] = (),
    *,
    own_markers: bool = False,
    reuse: bool = False,
) -> None:
    if path.is_relative_to(artifacts._REPO.resolve()):
        raise Refused(f"repository_path: {path} is inside the SonoranLab repository")
    for area in forbidden:
        if path.is_relative_to(area):
            raise Refused(f"forbidden_path: {path} is inside forbidden area {area}")
    if checkout := git_root(path):
        raise Refused(f"git_worktree: {path} is inside Git working tree {checkout}")
    for ancestor in (path, *path.parents):
        if ancestor == path and own_markers:
            continue
        if (ancestor / "raw").is_dir() and (ancestor / "processed").is_dir():
            raise Refused(f"nested_workspace: {path} is inside an existing job ({ancestor})")
        for marker in _MARKERS:
            if ancestor == path and reuse and marker == ".sonoranlab-staging":
                continue
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
    all other checks still apply, except for its own staging marker (mailbox reuse).
    """
    forbidden = tuple(Path(area).expanduser().resolve() for area in forbidden)
    root = _checked_root(root, forbidden)
    path = _checked_path(path)
    if path == root or not path.is_relative_to(root):
        raise Refused(f"outside_root: {path} must be strictly inside staging root {root}")
    _check_area(path, forbidden, reuse=not must_not_exist)
    git_areas = tuple(
        Path(os.environ[key]).expanduser().resolve() for key in ("GIT_DIR", "GIT_WORK_TREE") if os.environ.get(key)
    )
    for area in (artifacts._REPO.resolve(), *forbidden, *git_areas):
        if area.is_relative_to(path):
            raise Refused(f"forbidden_path: {path} would contain forbidden area {area}")
    if must_not_exist and path.exists():
        raise Refused(f"destination_exists: {path}: exists; staging directories are never reused")
    return path


def admit_repair(path: Path | str) -> Path:
    """Verify that path is an existing, safe, and valid repair workspace before mutation.

    Returns the canonical repair Path. Creates nothing, no lease, no mkdir.
    Refusal messages always begin with 'repair_not_admitted: <code_or_reason>'.
    """
    try:
        path = _checked_path(Path(path))
    except Refused as e:
        raise Refused(f"repair_not_admitted: {e}") from e

    repo = artifacts._REPO.resolve()
    if path.is_relative_to(repo):
        raise Refused(f"repair_not_admitted: repository_path: {path} is inside the SonoranLab repository")
    if repo.is_relative_to(path):
        raise Refused(f"repair_not_admitted: forbidden_path: {path} would contain repository {repo}")

    git_areas = tuple(
        Path(os.environ[key]).expanduser().resolve() for key in ("GIT_DIR", "GIT_WORK_TREE") if os.environ.get(key)
    )
    for area in git_areas:
        if path.is_relative_to(area):
            raise Refused(f"repair_not_admitted: forbidden_path: {path} is inside Git area {area}")
        if area.is_relative_to(path):
            raise Refused(f"repair_not_admitted: forbidden_path: {path} would contain Git area {area}")

    try:
        root = repair_root()
    except Refused as e:
        raise Refused(f"repair_not_admitted: {e}") from e

    if path == root or not path.is_relative_to(root):
        raise Refused(f"repair_not_admitted: outside_root: {path} must be strictly inside staging root {root}")

    if checkout := git_root(path):
        raise Refused(f"repair_not_admitted: git_worktree: {path} is inside Git working tree {checkout}")

    for ancestor in path.parents:
        if ancestor == root or not ancestor.is_relative_to(root):
            break
        if (ancestor / "raw").is_dir() and (ancestor / "processed").is_dir():
            raise Refused(f"repair_not_admitted: nested_workspace: {path} is inside an existing job ({ancestor})")
        for marker in _MARKERS:
            entry = ancestor / marker
            if entry.exists() or entry.is_symlink():
                raise Refused(
                    f"repair_not_admitted: nested_workspace: {path} is inside an existing job/repair ({entry})"
                )

    if not path.is_dir() or path.is_symlink():
        raise Refused(f"repair_not_admitted: not_a_directory: {path} is not a directory")

    staging_path = path / ".sonoranlab-staging"
    if staging_path.is_symlink() or not staging_path.is_file():
        raise Refused(f"repair_not_admitted: missing_staging_marker: {staging_path} is not a regular file")
    try:
        with staging_path.open("r", encoding="utf-8") as f:
            staging_data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise Refused(f"repair_not_admitted: invalid_staging_marker: {staging_path}: {e}") from e
    if not isinstance(staging_data, dict):
        raise Refused(f"repair_not_admitted: invalid_staging_marker: {staging_path} is not a JSON object")
    if staging_data.get("kind") != "repair":
        raise Refused(f"repair_not_admitted: invalid_staging_marker: {staging_path} kind is not 'repair'")
    staging_id = staging_data.get("id")
    if not isinstance(staging_id, str) or not staging_id:
        raise Refused(f"repair_not_admitted: invalid_staging_marker: {staging_path} missing or invalid id")

    repair_json = path / "repair.json"
    if repair_json.is_symlink() or not repair_json.is_file():
        raise Refused(f"repair_not_admitted: missing_repair_json: {repair_json} is not a regular file")
    try:
        with repair_json.open("r", encoding="utf-8") as f:
            repair_data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise Refused(f"repair_not_admitted: invalid_repair_json: {repair_json}: {e}") from e
    if not isinstance(repair_data, dict):
        raise Refused(f"repair_not_admitted: invalid_repair_json: {repair_json} is not a JSON object")
    if repair_data.get("schema") != "sonoranlab.repair/1":
        raise Refused(f"repair_not_admitted: invalid_repair_json: {repair_json} schema is not 'sonoranlab.repair/1'")
    if repair_data.get("repair_id") != staging_id:
        raise Refused(
            f"repair_not_admitted: marker_id_mismatch: {staging_path} id {staging_id!r} "
            f"does not match {repair_json} repair_id {repair_data.get('repair_id')!r}"
        )

    mapping_json = path / "mapping.json"
    if mapping_json.is_symlink() or not mapping_json.is_file():
        raise Refused(f"repair_not_admitted: missing_mapping_json: {mapping_json} is not a regular file")
    try:
        with mapping_json.open("r", encoding="utf-8") as f:
            mapping_data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise Refused(f"repair_not_admitted: invalid_mapping_json: {mapping_json}: {e}") from e
    if not isinstance(mapping_data, dict):
        raise Refused(f"repair_not_admitted: invalid_mapping_json: {mapping_json} is not a JSON object")

    for name in ("source", "workspace"):
        d = path / name
        if d.is_symlink() or not d.is_dir():
            raise Refused(f"repair_not_admitted: invalid_structure: {d} must be a directory without symlinks")

    profile_entry = repair_data.get("profile")
    if not isinstance(profile_entry, dict) or "path" not in profile_entry:
        raise Refused(f"repair_not_admitted: invalid_repair_json: {repair_json} missing profile.path")
    try:
        profile_path = _checked_path(Path(profile_entry["path"]))
        profile = load_profile(profile_path)
    except Exception as e:
        raise Refused(f"repair_not_admitted: invalid_profile: {e}") from e

    godot_project = Path(profile.export.godot_project).expanduser().resolve()
    palette_checkout = git_root(Path(profile.palette.source).expanduser().resolve())
    forbidden = (godot_project, *((palette_checkout,) if palette_checkout else ()))
    for area in forbidden:
        if path.is_relative_to(area):
            raise Refused(f"repair_not_admitted: forbidden_path: {path} is inside forbidden area {area}")
        if area.is_relative_to(path):
            raise Refused(f"repair_not_admitted: forbidden_path: {path} would contain forbidden area {area}")

    if parent_job := repair_data.get("parent_job"):
        parent_area = Path(parent_job).expanduser().resolve()
        if path.is_relative_to(parent_area):
            raise Refused(f"repair_not_admitted: forbidden_path: {path} is inside parent job {parent_area}")
        if parent_area.is_relative_to(path):
            raise Refused(f"repair_not_admitted: forbidden_path: {path} would contain parent job {parent_area}")

    return path


def verify_destination(path: Path, fd: int, *, forbidden: Iterable[Path] = ()) -> None:
    """Best-effort detection of a moved reservation or an area that became unsafe."""
    try:
        if os.readlink(f"/proc/self/fd/{fd}") != str(path) or _checked_path(path) != path:
            raise Refused("destination moved")
        info = os.stat(path, follow_symlinks=False)
        opened = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise Refused("destination replaced")
        _check_area(path, forbidden, own_markers=True)
    except (OSError, Refused) as e:
        raise Refused(f"race_detected: {path}: {e}") from e


def _open_directory(path: Path, *, create: bool = False) -> int:
    """Open an absolute directory from / without following any symlink."""
    fd = os.open("/", _DIRECTORY)
    try:
        for name in path.parts[1:]:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(name, mode=0o700, dir_fd=fd)
            child = os.open(name, _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError as e:
        os.close(fd)
        raise Refused(f"race_detected: {path}: unsafe directory component") from e


def open_destination(path: Path, *, root: Path, forbidden: Iterable[Path] = ()) -> tuple[Path, int]:
    """Validate and open a reusable mailbox via no-follow directory fds."""
    forbidden = tuple(forbidden)
    path = check_destination(path, root=root, forbidden=forbidden, must_not_exist=False)
    fd = _open_directory(path)
    try:
        verify_destination(path, fd, forbidden=forbidden)
        return path, fd
    except BaseException:
        os.close(fd)
        raise


def create_destination(
    path: Path,
    *,
    root: Path,
    kind: str,
    id: str,
    forbidden: Iterable[Path] = (),
) -> tuple[Path, int]:
    """Atomically reserve a fresh staging directory and mark it before other writes.

    The caller owns the returned fd and must verify it before reporting success.
    Failed reservations are retained; cleanup must never follow a raced pathname.
    """
    if kind not in ("repair", "child", "mailbox"):
        raise ValueError("unknown staging kind")
    forbidden = tuple(Path(area).expanduser().resolve() for area in forbidden)
    path = check_destination(path, root=root, forbidden=forbidden)
    root = Path(os.path.abspath(Path(root).expanduser()))
    fd = _open_directory(root, create=True)
    try:
        parts = path.relative_to(root).parts
        for index, name in enumerate(parts):
            try:
                os.mkdir(name, mode=0o700, dir_fd=fd)
            except FileExistsError as e:
                if index == len(parts) - 1:
                    raise Refused(f"destination_exists: {path}: staging directories are never reused") from e
            child = os.open(name, _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        marker = os.open(
            ".sonoranlab-staging",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=fd,
        )
        with os.fdopen(marker, "w") as stream:
            json.dump({"kind": kind, "id": id, "created_at": datetime.now(UTC).isoformat()}, stream)
            stream.write("\n")
        verify_destination(path, fd, forbidden=forbidden)
        return path, fd
    except BaseException as e:
        os.close(fd)
        if isinstance(e, OSError):
            raise Refused(f"race_detected: {path}: unsafe directory component") from e
        raise
