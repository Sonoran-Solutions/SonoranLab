"""Content-hash recovery of a job's repository-owned inputs from this repository's git history."""

import hashlib
import subprocess

import pytest

from sonoranlab import repo_inputs

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def git(repo, *args):
    return subprocess.run([*GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "profiles").mkdir(parents=True)
    git(root, "init", "-q")
    (root / "profiles/toy.yaml").write_bytes(b"id: toy\nversion: 1\n")
    (root / "profiles/link.yaml").symlink_to("toy.yaml")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "v1")
    v1 = git(root, "rev-parse", "HEAD")
    (root / "profiles/toy.yaml").write_bytes(b"id: toy\nversion: 2\n")
    git(root, "commit", "-qam", "v2")
    return root, v1, git(root, "rev-parse", "HEAD")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


GONE = "/home/someone/.local/state/orchestrator/runs/x/wt/final/profiles/toy.yaml"  # a removed worktree


def test_recovers_the_exact_recorded_bytes_from_a_removed_worktree_path(repo):
    root, v1, _ = repo
    got = repo_inputs.recover(GONE, sha(b"id: toy\nversion: 1\n"), v1, repo=root)
    assert got.data == b"id: toy\nversion: 1\n" and got.repo_path == "profiles/toy.yaml" and got.rev == v1
    ev = got.evidence()
    assert git(root, "cat-file", "blob", ev["blob"]) == "id: toy\nversion: 1"  # independently checkable
    assert ev["sha256"] == sha(b"id: toy\nversion: 1\n") and ev["recorded_path"] == GONE


@pytest.mark.parametrize(
    "case",
    [
        "other_revision",
        "wrong_sha",
        "unknown_commit",
        "short_rev",
        "dotdot",
        "symlink_blob",
        "directory",
        "nul",
        "types",
    ],
)
def test_anything_unverifiable_is_not_recovered(repo, case):
    root, v1, v2 = repo
    good = sha(b"id: toy\nversion: 1\n")
    args = {
        "other_revision": (GONE, good, v2),  # the file changed later: today's bytes never stand in for history
        "wrong_sha": (GONE, "0" * 64, v1),
        "unknown_commit": (GONE, good, "1" * 40),
        "short_rev": (GONE, good, v1[:12]),
        "dotdot": ("/x/../profiles/toy.yaml", good, v1),
        "symlink_blob": ("/gone/profiles/link.yaml", sha(b"toy.yaml"), v1),
        "directory": ("/gone/profiles", good, v1),
        "nul": ("/gone/profiles/toy.yaml\0", good, v1),
        "types": (None, good, v1),
    }[case]
    assert repo_inputs.recover(*args, repo=root) is None
