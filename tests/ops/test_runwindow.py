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
  stop) [ -f "$FAKE/slow_stop" ] && { touch "$FAKE/stopping"; sleep 2; }
        echo inactive > "$f"; [ -f "$FAKE/free_on_stop" ] && cp "$FAKE/free_on_stop" "$FAKE/proc/meminfo"; exit 0 ;;
  start) [ -f "$FAKE/start_fails" ] && exit 1; echo active > "$f" ;;
esac
"""


FAKE_PODMAN = r"""#!/usr/bin/env bash
# sonoranlab-comfy exists iff $FAKE/container exists; its .State.Status from $FAKE/status (default running);
# /queue body from $FAKE/queue (absent = curl fails). $FAKE/exists_rc / $FAKE/inspect_rc inject podman errors.
case "$1" in
  container) [ -f "$FAKE/exists_rc" ] && exit "$(cat "$FAKE/exists_rc")"; [ -f "$FAKE/container" ] ;;
  inspect) [ -f "$FAKE/inspect_rc" ] && { echo "Error: boom" >&2; exit "$(cat "$FAKE/inspect_rc")"; }
           cat "$FAKE/status" 2>/dev/null || echo running ;;
  exec) [ -f "$FAKE/queue" ] && cat "$FAKE/queue" || exit 7 ;;
esac
"""
BUSY = '{"queue_running": [[0, "p", {}, {}, []]], "queue_pending": []}'
IDLE = '{"queue_running": [], "queue_pending": []}'


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
    pm = tmp_path / "fake_podman.sh"
    pm.write_text(FAKE_PODMAN)
    work = tmp_path / "work"
    env = {**os.environ, "FAKE": str(tmp_path), "SONORANLAB_PROC": str(proc), "SONORANLAB_PODMAN": f"bash {pm}",
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

        def container(self, queue):
            (tmp_path / "container").write_text("")
            if queue is None:
                (tmp_path / "queue").unlink(missing_ok=True)
            else:
                (tmp_path / "queue").write_text(queue)

        def state(self):
            return (tmp_path / "units" / "llama-gemory.service").read_text().strip()

        def window(self, script, *extra, submits_prompt=False):
            """Start a guarded run in its own session that pauses llama-gemory; returns once the command started.
            submits_prompt: the command leaves a running prompt in the (idle at preflight) container."""
            if submits_prompt:
                self.container(IDLE)
                script = f"echo '{BUSY}' > {tmp_path}/queue; {script}"
            self.holder(100, "llama-gemory.service", 25)
            self.unit("llama-gemory.service", "active")
            (tmp_path / "free_on_stop").write_text(meminfo(70))
            p = subprocess.Popen([sys.executable, str(RUNWINDOW), "--own-cgroup", "", "--need-gib", "52",
                                  "--allow-pause", "llama-gemory", "--drain-s", "3", *extra, "--", "bash", "-c",
                                  f"touch {tmp_path}/started; {script}"],
                                 env=env, stderr=subprocess.PIPE, text=True, start_new_session=True)
            for _ in range(150):
                if (tmp_path / "started").exists() or p.poll() is not None:
                    break
                time.sleep(0.1)
            return p

        def calls(self):
            p = tmp_path / "calls"
            return p.read_text().splitlines() if p.exists() else []

        def run(self, *args, timeout=30):
            return subprocess.run([sys.executable, str(RUNWINDOW), "--own-cgroup", "", "--settle-s", "2",
                                   "--drain-s", "3", *args],
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


def test_restore_during_active_window_refuses_and_keeps_unit_stopped(fake):
    fake.holder(100, "llama-gemory.service", 25)
    fake.unit("llama-gemory.service", "active")
    (fake.root / "free_on_stop").write_text(meminfo(70))
    release = fake.root / "release"
    p = subprocess.Popen([sys.executable, str(RUNWINDOW), "--own-cgroup", "", "--need-gib", "52",
                          "--allow-pause", "llama-gemory", "--", "bash", "-c",
                          f"touch {fake.root}/started; while [ ! -f {release} ]; do sleep 0.1; done"],
                         env=fake.environ, stderr=subprocess.PIPE, text=True)
    for _ in range(100):
        if (fake.root / "started").exists():
            break
        time.sleep(0.1)
    unit = fake.root / "units" / "llama-gemory.service"
    assert unit.read_text().strip() == "inactive"           # paused by the live window
    r = fake.run("--restore")                                # e.g. recover.sh starting meanwhile
    assert r.returncode == 4 and "untouched" in r.stderr
    assert unit.read_text().strip() == "inactive"           # still paused: no competing model loaded
    assert (fake.root / "work" / "runwindow.state.json").exists()
    assert fake.calls().count("start llama-gemory.service") == 0
    release.write_text("")
    p.wait(20)
    assert p.returncode == 0
    assert unit.read_text().strip() == "active"             # restored by the owner of the window
    assert fake.calls().count("start llama-gemory.service") == 1
    assert fake.run("--restore").returncode == 0             # lock free again: restore runs (nothing to do)


def test_restore_after_killed_window_proceeds(fake):
    fake.holder(100, "llama-gemory.service", 25)
    fake.unit("llama-gemory.service", "active")
    (fake.root / "free_on_stop").write_text(meminfo(70))
    p = subprocess.Popen([sys.executable, str(RUNWINDOW), "--own-cgroup", "", "--need-gib", "52",
                          "--allow-pause", "llama-gemory", "--", "bash", "-c", f"touch {fake.root}/started; sleep 30"],
                         env=fake.environ, stderr=subprocess.PIPE, start_new_session=True)
    for _ in range(100):
        if (fake.root / "started").exists():
            break
        time.sleep(0.1)
    os.killpg(p.pid, signal.SIGKILL)                         # no cleanup runs; kernel releases the lock
    p.wait(10)
    assert (fake.root / "units" / "llama-gemory.service").read_text().strip() == "inactive"
    r = fake.run("--restore")
    assert r.returncode == 0, r.stderr
    assert (fake.root / "units" / "llama-gemory.service").read_text().strip() == "active"


# --- container-drain gate (review B1/S1/S2), stop race (S3), signal safety (S5), corrupt state -----------------


def test_ctrl_c_with_container_job_still_running_keeps_unit_stopped(fake):
    p = fake.window("sleep 30", submits_prompt=True)        # the ComfyUI prompt outlives the host-side client
    os.killpg(p.pid, signal.SIGINT)                         # terminal Ctrl-C hits the whole process group
    _, err = p.communicate(timeout=30)
    assert p.returncode != 0
    assert fake.state() == "inactive"                       # NOT restarted next to the running job
    assert "NOT restarting" in err
    assert (fake.root / "work" / "runwindow.state.json").exists()
    fake.container(IDLE)                                    # job finished
    assert fake.run("--restore").returncode == 0
    assert fake.state() == "active"


def test_restore_waits_for_container_job_to_drain(fake):
    p = fake.window("true", submits_prompt=True)
    time.sleep(1)
    fake.container(IDLE)                                    # job finishes during the drain wait
    _, err = p.communicate(timeout=30)
    assert p.returncode == 0, err
    assert "waiting up to" in err and fake.state() == "active"


def test_restore_refuses_while_orphaned_job_runs(fake):
    fake.unit("llama-gemory.service", "inactive")
    (fake.root / "work").mkdir()
    (fake.root / "work" / "runwindow.state.json").write_text(json.dumps({"paused": ["llama-gemory.service"]}))
    fake.container(BUSY)                                    # e.g. runwindow was SIGKILLed, container kept going
    assert fake.run("--restore").returncode == 1
    assert fake.state() == "inactive" and (fake.root / "work" / "runwindow.state.json").exists()
    fake.container(None)                                    # running container that can't report its queue
    assert fake.run("--restore").returncode == 1 and fake.state() == "inactive"
    fake.container(IDLE)
    assert fake.run("--restore").returncode == 0 and fake.state() == "active"


def test_auto_restore_before_new_run_also_waits_for_drain(fake):
    fake.unit("llama-gemory.service", "inactive")
    (fake.root / "work").mkdir()
    (fake.root / "work" / "runwindow.state.json").write_text(json.dumps({"paused": ["llama-gemory.service"]}))
    (fake.root / "proc" / "meminfo").write_text(meminfo(80))
    fake.container(BUSY)
    r = fake.run("--need-gib", "20", *marker_cmd(fake.root / "ran"))
    assert r.returncode == 3 and not (fake.root / "ran").exists() and fake.state() == "inactive"


def test_signal_during_slow_stop_is_still_restored(fake):
    (fake.root / "slow_stop").write_text("")
    fake.holder(100, "llama-gemory.service", 25)
    fake.unit("llama-gemory.service", "active")
    (fake.root / "free_on_stop").write_text(meminfo(70))
    p = subprocess.Popen([sys.executable, str(RUNWINDOW), "--own-cgroup", "", "--need-gib", "52", "--drain-s", "3",
                          "--allow-pause", "llama-gemory", "--", "true"], env=fake.environ,
                         stderr=subprocess.PIPE, text=True)
    for _ in range(100):
        if (fake.root / "stopping").exists():
            break
        time.sleep(0.1)
    p.send_signal(signal.SIGINT)                            # arrives while `systemctl stop` is in progress
    _, err = p.communicate(timeout=30)
    assert p.returncode == 130
    assert fake.state() == "active", err                   # it was recorded before the stop, so it is restored


def test_second_signal_does_not_abort_restore(fake):
    p = fake.window("sleep 30", "--drain-s", "4", submits_prompt=True)
    assert fake.state() == "inactive"                       # the window really paused the unit
    p.send_signal(signal.SIGTERM)                           # first signal: forwarded to the command
    time.sleep(1)
    fake.container(IDLE)
    p.send_signal(signal.SIGTERM)                           # second signal during the drain/restore: ignored
    _, err = p.communicate(timeout=30)
    assert fake.state() == "active", err


def test_corrupt_state_file_fails_closed_with_guidance(fake):
    (fake.root / "work").mkdir()
    (fake.root / "work" / "runwindow.state.json").write_text("{not json")
    r = fake.run("--restore")
    assert r.returncode == 1 and "unreadable" in r.stderr and "Traceback" not in r.stderr


# --- queue lifecycle (DYL-97): preflight queue check, drain even with nothing paused --------------------------


def test_preflight_refuses_while_orphaned_prompt_runs(fake):
    (fake.root / "proc" / "meminfo").write_text(meminfo(100))   # memory alone would say "fits"
    fake.container(BUSY)                                         # prompt left behind by a killed client
    r = fake.run("--need-gib", "20", *marker_cmd(fake.root / "ran"))
    assert r.returncode == 3 and not (fake.root / "ran").exists()
    assert "overlap" in r.stderr
    assert fake.run("--need-gib", "20", "--dry-run").returncode == 3
    fake.container(None)                                         # running but queue unreadable: fail closed
    assert fake.run("--need-gib", "20", *marker_cmd(fake.root / "ran")).returncode == 3
    fake.container(IDLE)
    assert fake.run("--need-gib", "20", *marker_cmd(fake.root / "ran")).returncode == 0
    assert (fake.root / "ran").exists()


def test_window_without_pauses_still_drains_before_exiting(fake):
    (fake.root / "proc" / "meminfo").write_text(meminfo(100))
    fake.container(IDLE)
    # The command exits but leaves its prompt running in the container (as an interrupted client would).
    p = subprocess.Popen([sys.executable, str(RUNWINDOW), "--own-cgroup", "", "--need-gib", "20", "--drain-s", "10",
                          "--", "bash", "-c", f"echo '{BUSY}' > {fake.root}/queue; touch {fake.root}/started"],
                         env=fake.environ, stderr=subprocess.PIPE, text=True)
    for _ in range(100):
        if (fake.root / "started").exists():
            break
        time.sleep(0.1)
    time.sleep(1)
    assert p.poll() is None                                      # still owns the window while the prompt runs
    assert fake.run("--need-gib", "20", *marker_cmd(fake.root / "ran")).returncode == 4
    fake.container(IDLE)
    _, err = p.communicate(timeout=30)
    assert p.returncode == 0, err
    assert "waiting up to" in err
    assert not any(c.startswith(("stop", "start")) for c in fake.calls())


def test_window_without_pauses_that_never_drains_fails_and_blocks_next_run(fake):
    (fake.root / "proc" / "meminfo").write_text(meminfo(100))
    fake.container(IDLE)
    r = fake.run("--need-gib", "20", "--", "bash", "-c", f"echo '{BUSY}' > {fake.root}/queue")
    assert r.returncode == 1 and "New run windows refuse" in r.stderr
    r = fake.run("--need-gib", "20", *marker_cmd(fake.root / "ran"))
    assert r.returncode == 3 and not (fake.root / "ran").exists()


# --- fail-closed container inspection (DYL-97) ----------------------------------------------------------------


@pytest.mark.parametrize("setup", ["exists_rc=125", "inspect_rc=125", "status=paused", "status=stopping",
                                   "status=removing", "status=", "podman_missing"])
def test_unconfirmed_container_state_refuses(fake, setup):
    (fake.root / "proc" / "meminfo").write_text(meminfo(100))
    fake.container(IDLE)                                         # even an idle queue must not be reached
    env = dict(fake.environ)
    if setup == "podman_missing":
        env["SONORANLAB_PODMAN"] = str(fake.root / "no-such-podman")
    else:
        key, val = setup.split("=")
        (fake.root / key).write_text(val)
    r = subprocess.run([sys.executable, str(RUNWINDOW), "--own-cgroup", "", "--need-gib", "20", "--drain-s", "1",
                        *marker_cmd(fake.root / "ran")], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 3, r.stderr
    assert not (fake.root / "ran").exists()


@pytest.mark.parametrize("status", ["exited", "stopped", "created", "configured"])
def test_confirmed_stopped_container_is_idle(fake, status):
    (fake.root / "proc" / "meminfo").write_text(meminfo(100))
    fake.container(None)                                         # queue unreadable, but nothing is running
    (fake.root / "status").write_text(status)
    r = fake.run("--need-gib", "20", *marker_cmd(fake.root / "ran"))
    assert r.returncode == 0, r.stderr


def test_restore_keeps_units_stopped_when_podman_errors(fake):
    fake.unit("llama-gemory.service", "inactive")
    (fake.root / "work").mkdir()
    (fake.root / "work" / "runwindow.state.json").write_text(json.dumps({"paused": ["llama-gemory.service"]}))
    (fake.root / "exists_rc").write_text("125")                  # e.g. podman storage locked / broken
    assert fake.run("--restore").returncode == 1
    assert fake.state() == "inactive"
    (fake.root / "exists_rc").unlink()                           # confirmed absent: safe
    assert fake.run("--restore").returncode == 0 and fake.state() == "active"
