"""Regression tests for env/runwindow.py against a fake /proc and a fake `systemctl --user`."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RUNWINDOW = REPO / "env" / "runwindow.py"
GIB_KB = 1 << 20  # KiB per GiB

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
# usage: fake_systemctl.sh <verb> <unit>; state in $FAKE/units/<unit>, calls logged to $FAKE/calls
echo "$*" >> "$FAKE/calls"
f="$FAKE/units/$2"
case "$1" in
  is-active) s=$(cat "$f" 2>/dev/null || echo inactive); echo "$s"; [ "$s" = active ] ;;
  stop) echo inactive > "$f"; [ -f "$FAKE/free_on_stop" ] && cp "$FAKE/free_on_stop" "$FAKE/proc/meminfo"; exit 0 ;;
  start) [ -f "$FAKE/start_fails" ] && exit 1; echo active > "$f" ;;
esac
"""


def meminfo(avail_gib: float) -> str:
    return f"MemTotal: {112 * GIB_KB} kB\nMemAvailable: {int(avail_gib * GIB_KB)} kB\n"


@pytest.fixture
def fake(tmp_path):
    proc = tmp_path / "proc"
    (proc / "pressure").mkdir(parents=True)
    (proc / "meminfo").write_text(meminfo(40))
    (proc / "pressure" / "memory").write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=1\n")
    (tmp_path / "units").mkdir()
    sc = tmp_path / "fake_systemctl.sh"
    sc.write_text(FAKE_SYSTEMCTL)
    work = tmp_path / "work"
    env = {**os.environ, "FAKE": str(tmp_path), "SONORANLAB_PROC": str(proc),
           "SONORANLAB_SYSTEMCTL": f"bash {sc}", "SONORANLAB_WORK": str(work)}

    class F:
        root, environ = tmp_path, env

        def holder(self, pid, unit, gtt_gib, comm="llama-server"):
            d = proc / str(pid)
            (d / "fdinfo").mkdir(parents=True)
            (d / "fdinfo" / "7").write_text(f"drm-driver:\tamdgpu\ndrm-client-id:\t{pid}\n"
                                            f"drm-memory-gtt:\t{int(gtt_gib * GIB_KB)} KiB\ndrm-memory-vram:\t0 KiB\n")
            (d / "cgroup").write_text(f"0::/user.slice/user@1000.service/app.slice/{unit}\n")
            (d / "comm").write_text(comm + "\n")
            (d / "status").write_text("VmRSS:\t102400 kB\n")

        def unit(self, name, state):
            (tmp_path / "units" / name).write_text(state)

        def calls(self):
            p = tmp_path / "calls"
            return p.read_text().splitlines() if p.exists() else []

        def run(self, *args, timeout=30):
            return subprocess.run([sys.executable, str(RUNWINDOW), "--own-cgroup", "", "--settle-s", "2", *args],
                                  env=env, capture_output=True, text=True, timeout=timeout)

    return F()


def marker_cmd(path, rc=0):
    return ["--", "bash", "-c", f"touch {path}; exit {rc}"]


def test_fits_runs_without_pausing_and_propagates_rc(fake):
    fake.holder(100, "llama-gemory.service", 19)
    fake.unit("llama-gemory.service", "active")
    r = fake.run("--need-gib", "20", "--margin-gib", "4", "--allow-pause", "llama-gemory", *marker_cmd(fake.root / "ran", 7))
    assert r.returncode == 7
    assert (fake.root / "ran").exists()
    assert not any(c.startswith(("stop", "start")) for c in fake.calls())


def test_refuses_without_approval_and_touches_nothing(fake):
    fake.holder(100, "llama-gemory.service", 19)
    fake.unit("llama-gemory.service", "active")
    r = fake.run("--need-gib", "52", *marker_cmd(fake.root / "ran"))
    assert r.returncode == 3
    assert not (fake.root / "ran").exists()
    assert fake.calls() == []
    assert "llama-gemory.service" in r.stderr and "--allow-pause" in r.stderr


def test_refuses_when_approved_pause_is_not_enough(fake):
    fake.holder(100, "llama-gemory.service", 5)
    fake.holder(200, "app-com.example.Browser.scope", 30, comm="browser")
    fake.unit("llama-gemory.service", "active")
    r = fake.run("--need-gib", "52", "--allow-pause", "llama-gemory", *marker_cmd(fake.root / "ran"))
    assert r.returncode == 3
    assert not (fake.root / "ran").exists()
    assert not any(c.startswith("stop") for c in fake.calls())
    assert "app-com.example.Browser.scope" in r.stderr


def test_approved_pause_runs_and_restores(fake):
    fake.holder(100, "llama-gemory.service", 25)
    fake.unit("llama-gemory.service", "active")
    (fake.root / "free_on_stop").write_text(meminfo(70))
    r = fake.run("--need-gib", "52", "--allow-pause", "llama-gemory", *marker_cmd(fake.root / "ran"))
    assert r.returncode == 0, r.stderr
    assert (fake.root / "ran").exists()
    calls = fake.calls()
    assert calls.index("stop llama-gemory.service") < calls.index("start llama-gemory.service")
    assert (fake.root / "units" / "llama-gemory.service").read_text().strip() == "active"
    assert not (fake.root / "work" / "runwindow.state.json").exists()


def test_failing_command_still_restores(fake):
    fake.holder(100, "llama-gemory.service", 25)
    fake.unit("llama-gemory.service", "active")
    (fake.root / "free_on_stop").write_text(meminfo(70))
    r = fake.run("--need-gib", "52", "--allow-pause", "llama-gemory", *marker_cmd(fake.root / "ran", 5))
    assert r.returncode == 5
    assert "start llama-gemory.service" in fake.calls()


def test_originally_inactive_unit_is_not_started(fake):
    fake.holder(100, "llama-gemory.service", 19)
    fake.unit("llama-gemory.service", "inactive")
    r = fake.run("--need-gib", "52", "--allow-pause", "llama-gemory", *marker_cmd(fake.root / "ran"))
    assert r.returncode == 3  # memory never frees, settle times out
    assert not any(c.startswith(("stop", "start")) for c in fake.calls())


def test_settle_timeout_restores_paused_unit(fake):
    fake.holder(100, "llama-gemory.service", 25)
    fake.unit("llama-gemory.service", "active")
    (fake.root / "free_on_stop").write_text(meminfo(45))  # frees some, not enough
    r = fake.run("--need-gib", "52", "--allow-pause", "llama-gemory", *marker_cmd(fake.root / "ran"))
    assert r.returncode == 3
    assert not (fake.root / "ran").exists()
    assert fake.calls()[-2:] == ["start llama-gemory.service", "is-active llama-gemory.service"]


def test_sigterm_during_run_forwards_and_restores(fake):
    fake.holder(100, "llama-gemory.service", 25)
    fake.unit("llama-gemory.service", "active")
    (fake.root / "free_on_stop").write_text(meminfo(70))
    p = subprocess.Popen([sys.executable, str(RUNWINDOW), "--own-cgroup", "", "--need-gib", "52",
                          "--allow-pause", "llama-gemory", "--", "bash", "-c",
                          f"trap 'touch {fake.root}/child_got_term; exit 143' TERM; touch {fake.root}/started; sleep 30 & wait"],
                         env=fake.environ, stderr=subprocess.PIPE, text=True)
    for _ in range(100):
        if (fake.root / "started").exists():
            break
        time.sleep(0.1)
    p.send_signal(signal.SIGTERM)
    p.wait(20)
    assert p.returncode == 128 + signal.SIGTERM
    assert (fake.root / "child_got_term").exists()
    assert "start llama-gemory.service" in fake.calls()


def test_state_from_killed_run_is_restored_first(fake):
    fake.unit("llama-gemory.service", "inactive")
    (fake.root / "work").mkdir()
    (fake.root / "work" / "runwindow.state.json").write_text(json.dumps({"paused": ["llama-gemory.service"]}))
    (fake.root / "proc" / "meminfo").write_text(meminfo(80))
    r = fake.run("--need-gib", "20", *marker_cmd(fake.root / "ran"))
    assert r.returncode == 0
    assert fake.calls()[0] == "start llama-gemory.service"
    assert not (fake.root / "work" / "runwindow.state.json").exists()


def test_restore_subcommand_and_failed_restore_keeps_state(fake):
    (fake.root / "work").mkdir()
    state = fake.root / "work" / "runwindow.state.json"
    state.write_text(json.dumps({"paused": ["llama-gemory.service"]}))
    (fake.root / "start_fails").write_text("")
    assert fake.run("--restore").returncode == 1
    assert state.exists()
    (fake.root / "start_fails").unlink()
    assert fake.run("--restore").returncode == 0
    assert not state.exists()


def test_high_memory_pressure_refuses(fake):
    (fake.root / "proc" / "meminfo").write_text(meminfo(100))
    (fake.root / "proc" / "pressure" / "memory").write_text("some avg10=35.00 avg60=1 avg300=1 total=1\n")
    r = fake.run("--need-gib", "20", *marker_cmd(fake.root / "ran"))
    assert r.returncode == 3 and not (fake.root / "ran").exists()


def test_concurrent_window_is_refused(fake):
    (fake.root / "proc" / "meminfo").write_text(meminfo(100))
    p = subprocess.Popen([sys.executable, str(RUNWINDOW), "--own-cgroup", "", "--need-gib", "1", "--",
                          "bash", "-c", f"touch {fake.root}/started; sleep 5"], env=fake.environ, stderr=subprocess.PIPE)
    for _ in range(100):
        if (fake.root / "started").exists():
            break
        time.sleep(0.1)
    r = fake.run("--need-gib", "1", *marker_cmd(fake.root / "ran"))
    p.kill()
    p.wait()
    assert r.returncode == 4 and not (fake.root / "ran").exists()


def test_dry_run_never_pauses_or_runs(fake):
    fake.holder(100, "llama-gemory.service", 25)
    fake.unit("llama-gemory.service", "active")
    (fake.root / "free_on_stop").write_text(meminfo(70))
    r = fake.run("--need-gib", "52", "--allow-pause", "llama-gemory", "--dry-run")
    assert r.returncode == 0
    assert not any(c.startswith(("stop", "start")) for c in fake.calls())
