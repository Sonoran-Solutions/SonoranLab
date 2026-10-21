"""Real local leases and flocks; no services or GPU."""

import fcntl
import json
import os
from contextlib import ExitStack
from types import SimpleNamespace

import pytest


@pytest.fixture
def lease_window(tmp_path, monkeypatch):
    with ExitStack() as stack:

        def create(work=None, need_gib=60):
            work = work or tmp_path / "guard-work"
            work.mkdir(parents=True, exist_ok=True)
            proc = tmp_path / "proc"
            boot = proc / "sys/kernel/random/boot_id"
            boot.parent.mkdir(parents=True, exist_ok=True)
            boot.write_text("test-boot\n")
            monkeypatch.setenv("SONORANLAB_WORK", str(work))
            monkeypatch.setenv("SONORANLAB_PROC", str(proc))
            monkeypatch.setenv("SONORANLAB_RUNWINDOW", "12345")
            monkeypatch.setenv("SONORANLAB_RUNWINDOW_NONCE", "a" * 32)
            monkeypatch.setattr(os, "getppid", lambda: 12345)
            lock = stack.enter_context((work / "runwindow.lock").open("w"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            data = {
                "schema": "sonoranlab.runwindow-lease/1",
                "pid": 12345,
                "need_gib": need_gib,
                "margin_gib": 8,
                "nonce": "a" * 32,
                "started": 1,
                "boot_id": "test-boot",
            }
            path = work / "runwindow.lease.json"
            path.write_text(json.dumps(data))
            path.chmod(0o600)
            return SimpleNamespace(data=data, path=path, lock=lock, boot=boot)

        yield create
