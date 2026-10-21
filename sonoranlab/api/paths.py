"""Confinement for caller paths. No directories are created by discovery or resolution.

Inputs relative to cwd must be regular existing files. Outputs relative to STATE must
have an existing directory parent and an absent leaf; callers create exclusively.

ponytail: returned Paths confine the layout at check time. Concurrent same-UID
ancestor renames require directory-fd operations or mount isolation at use time.
"""

from __future__ import annotations

import os
from pathlib import Path

from .registry import Context, ToolError


def _path(path: str | Path) -> Path:
    if not str(path) or "\x00" in str(path):
        raise ToolError("path_refused", "Empty paths and NUL bytes are refused")
    path = Path(path).expanduser()
    if ".." in path.parts:
        raise ToolError("path_refused", "Path traversal is refused")
    return path


def state_root() -> Path:
    """Canonical state root; it need not exist yet."""
    return _path(os.environ.get("SONORANLAB_STATE", "~/sonoranlab-work/mcp")).resolve()


def read_roots() -> tuple[Path, ...]:
    """Canonical configured read roots, excluding empty path-list entries."""
    roots = os.environ.get("SONORANLAB_READ_ROOTS", "~/sonoranlab-work").split(os.pathsep)
    return tuple(_path(root).resolve() for root in roots if root)


def _game_roots() -> tuple[Path, ...]:
    return tuple(profile.export.godot_project.resolve() for profile in Context().profiles())


def _confine(path: Path, roots: tuple[Path, ...]) -> Path:
    """Check lexical and real paths, so an inside link cannot escape into another root."""
    lexical = Path(os.path.abspath(path))
    matched = tuple(root for root in roots if lexical.is_relative_to(root))
    if not matched:
        raise ToolError("path_refused", "Path is outside the permitted roots")
    try:
        # Check containment before existence so a missing target cannot hide an escape.
        resolved = lexical.resolve()
    except (RuntimeError, OSError) as exc:
        raise ToolError("path_refused", "Unresolvable or escaping symlink") from exc
    if not any(resolved.is_relative_to(root) for root in matched):
        raise ToolError("path_refused", "Symlink escapes the permitted root")
    if any(lexical.is_relative_to(root) or resolved.is_relative_to(root) for root in _game_roots()):
        raise ToolError("path_refused", "Game checkout is protected; only the profile loader may read its palette")
    try:
        return lexical.resolve(strict=True)
    except FileNotFoundError as exc:
        if any(part.is_symlink() for part in (lexical, *lexical.parents)):
            raise ToolError("path_refused", "Dangling symlink is refused") from exc
        raise
    except (RuntimeError, OSError) as exc:
        raise ToolError("path_refused", "Unresolvable or non-directory ancestor") from exc


def resolve_input(path: str | Path) -> Path:
    """Resolve a regular file inside READ roots or STATE; game reads are refused."""
    resolved = _confine(_path(path), (*read_roots(), state_root()))
    if not resolved.is_file():
        raise ToolError("path_refused", "Input must be a regular file")
    return resolved


def resolve_output(path: str | Path) -> Path:
    """Resolve an absent output leaf under STATE. Creation must use O_EXCL/mkdir."""
    path = _path(path)
    if not path.is_absolute():
        path = state_root() / path
    parent = _confine(path.parent, (state_root(),))
    if not parent.is_dir():
        raise ToolError("path_refused", "Output parent must be a directory")
    output = parent / path.name
    if output.is_symlink():
        raise ToolError("path_refused", "Symlink output is refused")
    if any(output.is_relative_to(root) for root in _game_roots()):
        raise ToolError("path_refused", "Game checkout is protected")
    if output.exists():
        raise FileExistsError("Output already exists")
    return output
