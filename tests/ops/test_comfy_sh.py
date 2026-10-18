"""Regression tests for env/comfy.sh's replace guard, using a fake podman."""

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
COMFY = REPO / "env" / "comfy.sh"

FAKE_PODMAN = r"""#!/usr/bin/env bash
# state: $FAKE/exists, $FAKE/running (true/false), $FAKE/queue (JSON or absent = curl fails); calls -> $FAKE/calls
echo "$*" >> "$FAKE/calls"
case "$1" in
  container) [ -f "$FAKE/exists" ] ;;
  inspect) cat "$FAKE/running" 2>/dev/null || echo false ;;
  image) exit 0 ;;
  rm) rm -f "$FAKE/exists"; echo false > "$FAKE/running" ;;
  run) touch "$FAKE/exists"; echo true > "$FAKE/running"; echo cid ;;
  logs) exit 0 ;;
  exec)
    case "$*" in
      *"/queue"*) [ -f "$FAKE/queue" ] && cat "$FAKE/queue" || exit 7 ;;
      *"/system_stats"*) echo '{"system":{}}' ;;
    esac ;;
esac
"""


@pytest.fixture
def fake(tmp_path):
    p = tmp_path / "podman.sh"
    p.write_text(FAKE_PODMAN)
    env = {**os.environ, "FAKE": str(tmp_path), "SONORANLAB_PODMAN": f"bash {p}", "SONORANLAB_SCOPE": "0",
           "SONORANLAB_MODELS": str(tmp_path / "models"), "SONORANLAB_WORK": str(tmp_path / "work")}

    class F:
        root = tmp_path

        def container(self, running: bool, queue: str | None):
            (tmp_path / "exists").write_text("")
            (tmp_path / "running").write_text("true" if running else "false")
            if queue is not None:
                (tmp_path / "queue").write_text(queue)

        def run(self, *args):
            return subprocess.run(["bash", str(COMFY), *args], env=env, capture_output=True, text=True, timeout=30)

        def destroyed(self) -> bool:
            calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
            return any(c.startswith("rm ") for c in calls)

    return F()


IDLE = '{"queue_running": [], "queue_pending": []}'
BUSY = '{"queue_running": [[0, "abc", {}, {}, []]], "queue_pending": []}'


def test_up_without_container_starts(fake):
    r = fake.run("up")
    assert r.returncode == 0, r.stderr


def test_up_refuses_running_container_without_replace(fake):
    fake.container(True, IDLE)
    r = fake.run("up")
    assert r.returncode == 5 and "--replace" in r.stderr
    assert not fake.destroyed()


def test_up_replace_idle_container(fake):
    fake.container(True, IDLE)
    r = fake.run("up", "--replace")
    assert r.returncode == 0, r.stderr
    assert fake.destroyed()


def test_up_replace_refuses_busy_container(fake):
    fake.container(True, BUSY)
    r = fake.run("up", "--replace")
    assert r.returncode == 5 and "running or pending" in r.stderr
    assert not fake.destroyed()


def test_up_replace_refuses_when_queue_unreadable(fake):
    fake.container(True, None)
    r = fake.run("up", "--replace", "--offline")
    assert r.returncode == 5 and "cannot read" in r.stderr
    assert not fake.destroyed()


def test_up_force_replaces_busy_container(fake):
    fake.container(True, BUSY)
    r = fake.run("up", "--force")
    assert r.returncode == 0, r.stderr
    assert fake.destroyed()


def test_stopped_container_is_replaced_without_flags(fake):
    fake.container(False, None)
    assert fake.run("up").returncode == 0


def test_down_refuses_busy_and_force_overrides(fake):
    fake.container(True, BUSY)
    assert fake.run("down").returncode == 5 and not fake.destroyed()
    assert fake.run("down", "--force").returncode == 0 and fake.destroyed()


def test_unknown_up_option_rejected(fake):
    assert fake.run("up", "--bogus").returncode == 2

