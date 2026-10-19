"""Regression tests for env/comfy.sh's replace guard, using a fake podman."""

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
COMFY = REPO / "env" / "comfy.sh"

FAKE_PODMAN = r"""#!/usr/bin/env bash
# state: $FAKE/exists, $FAKE/status (.State.Status), $FAKE/queue (JSON or absent = curl fails); calls -> $FAKE/calls
# $FAKE/exists_rc / $FAKE/inspect_rc inject podman errors.
echo "$*" >> "$FAKE/calls"
case "$1" in
  container) [ -f "$FAKE/exists_rc" ] && exit "$(cat "$FAKE/exists_rc")"; [ -f "$FAKE/exists" ] ;;
  inspect) [ -f "$FAKE/inspect_rc" ] && { echo "Error: boom" >&2; exit "$(cat "$FAKE/inspect_rc")"; }
           cat "$FAKE/status" 2>/dev/null || echo exited ;;
  image) exit 0 ;;
  rm) rm -f "$FAKE/exists" "$FAKE/exists_rc" "$FAKE/inspect_rc"; echo exited > "$FAKE/status" ;;
  run) touch "$FAKE/exists"; echo running > "$FAKE/status"; echo cid ;;
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
            (tmp_path / "status").write_text("running" if running else "exited")
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



def test_up_and_down_refuse_while_a_run_window_holds_the_lock(fake):
    import fcntl
    fake.container(True, IDLE)
    (fake.root / "work").mkdir(exist_ok=True)
    with open(fake.root / "work" / "runwindow.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)    # a guarded smoke run is active
        r = fake.run("up", "--replace")
        assert r.returncode == 5 and "run window" in r.stderr and not fake.destroyed()
        assert fake.run("down", "--force").returncode == 5 and not fake.destroyed()
    assert fake.run("up", "--replace").returncode == 0 and fake.destroyed()


# --- fail-closed container inspection (DYL-97) ----------------------------------------------------------------


@pytest.mark.parametrize("setup", ["exists_rc=125", "inspect_rc=125", "status=paused", "status=stopping",
                                   "status=removing", "status="])
def test_unconfirmed_state_is_never_treated_as_idle(fake, setup):
    fake.container(True, IDLE)
    key, val = setup.split("=")
    (fake.root / key).write_text(val)
    for args in (("up",), ("up", "--replace"), ("down",)):
        r = fake.run(*args)
        assert r.returncode == 5 and "cannot confirm" in r.stderr, (args, r.stderr)
    assert not fake.destroyed()
    assert fake.run("up", "--force").returncode == 0 and fake.destroyed()   # explicit override still works

