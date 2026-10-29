"""Recover a job's repository-owned inputs (its profile) by content hash from this repository's git history.

Older manifests bind absolute paths, some inside worktrees that no longer exist or outside the permitted read roots.
The bytes a job used are still in git: the blob at the job's recorded commit, at the same repository path. A recovery
must hash to the manifest's recorded sha256. Nothing outside this repository's object store is read, no manifest is
rewritten, and a recovery proves only that the recorded input is that exact historical blob: never that it equals
today's file, and never anything about an approval (reviews stay bound to their own artifact paths and hashes).
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sonoranlab.artifacts import _REPO

_SHA = re.compile(r"[0-9a-f]{64}")
_REV = re.compile(r"[0-9a-f]{40}")
_MAX_BYTES = 4 * 1024 * 1024
_MAX_SUFFIX = 8  # repository paths are shallow; a recorded path's last components name the file


@dataclass(frozen=True)
class Recovered:
    path: str  # as recorded in the manifest
    sha256: str
    rev: str
    repo_path: str
    blob: str
    data: bytes = field(repr=False)

    def evidence(self) -> dict:
        """Independently checkable: `git cat-file blob <blob> | sha256sum` == sha256, and `<rev>:<repo_path>` is it."""
        return {
            "recorded_path": self.path,
            "sha256": self.sha256,
            "git_rev": self.rev,
            "repo_path": self.repo_path,
            "blob": self.blob,
            "source": "git object store of this repository",
        }


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=10)


def recover(path: object, sha256: object, rev: object, *, repo: Path | None = None) -> Recovered | None:
    """The blob at `rev` whose repository path is a suffix of the recorded `path` and whose sha256 is `sha256`,
    or None. Anything malformed or unverifiable is None."""
    repo = repo or _REPO
    if not (isinstance(path, str) and isinstance(sha256, str) and isinstance(rev, str)):
        return None
    if not (_SHA.fullmatch(sha256) and _REV.fullmatch(rev)) or "\0" in path:
        return None
    parts = [p for p in PurePosixPath(path).parts if p != "/"]
    if not parts or any(p in {".", ".."} for p in parts):
        return None
    try:
        if _git(repo, "cat-file", "-e", f"{rev}^{{commit}}").returncode != 0:
            return None
        for n in range(1, min(len(parts), _MAX_SUFFIX) + 1):
            rel = "/".join(parts[-n:])
            listed = _git(repo, "ls-tree", "-l", "--full-tree", rev, "--", rel)
            line = listed.stdout.decode(errors="replace").strip()
            if listed.returncode != 0 or not line or "\n" in line:
                continue
            meta, _, name = line.partition("\t")
            mode, kind, blob, size = meta.split()
            if name != rel or kind != "blob" or mode not in {"100644", "100755"} or int(size) > _MAX_BYTES:
                continue
            data = _git(repo, "cat-file", "blob", blob)
            if data.returncode == 0 and hashlib.sha256(data.stdout).hexdigest() == sha256:
                return Recovered(path, sha256, rev, rel, blob, data.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None
