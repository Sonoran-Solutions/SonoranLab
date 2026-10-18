#!/usr/bin/env python3
"""Memory run-window guard for heavy inference on the shared Strix Halo box (stdlib only).

Unified memory is shared with the desktop and other AI services; the container's 64 GiB cap does not stop
systemd-oomd from killing *other* scopes when the host runs out. This wrapper:

1. inventories who holds GPU memory (amdgpu fdinfo, per process, mapped to its systemd unit) and checks
   MemAvailable and memory pressure (PSI) against what the workload needs;
2. fails safely (exit 3, nothing stopped, nothing run) when the run does not fit;
3. pauses only systemd *user* units the caller explicitly approved with --allow-pause, and only if needed;
4. runs the command, forwarding INT/TERM/HUP to it;
5. restores every paused unit to its original state afterwards, including on failure and on signals. If this
   process is SIGKILLed, the paused units are listed in a state file and `--restore` (run by recover.sh) brings
   them back.

    python3 env/runwindow.py --need-gib 52 [--allow-pause llama-gemory] -- bash env/smoke.sh WF TAG
    python3 env/runwindow.py --need-gib 52 --dry-run          # report only
    python3 env/runwindow.py --restore                        # restore units left paused by a killed run
                                                              # (refuses with exit 4 while any window is live; waits
                                                              #  --drain-s for the container job to finish first)

Exit codes: the command's own code; 3 preflight refused; 4 another run window is active; 128+N on signal N.
Test hooks: SONORANLAB_PROC (proc root), SONORANLAB_SYSTEMCTL (systemctl command), SONORANLAB_WORK (state dir).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

GIB = 1 << 30
PROC = Path(os.environ.get("SONORANLAB_PROC", "/proc"))
SYSTEMCTL = shlex.split(os.environ.get("SONORANLAB_SYSTEMCTL", "systemctl --user"))
WORK = Path(os.environ.get("SONORANLAB_WORK", Path.home() / "sonoranlab-work"))
STATE = WORK / "runwindow.state.json"
LOCK = WORK / "runwindow.lock"


def log(msg: str) -> None:
    try:
        print(f"[runwindow] {msg}", file=sys.stderr, flush=True)
    except OSError:
        pass  # terminal gone (EIO/EPIPE): never let logging abort a restore


def mem_available() -> int:
    for line in (PROC / "meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("MemAvailable not found")


def psi_some_avg10() -> float:
    try:
        for line in (PROC / "pressure" / "memory").read_text().splitlines():
            if line.startswith("some"):
                return float(dict(kv.split("=") for kv in line.split()[1:])["avg10"])
    except (OSError, KeyError, ValueError):
        pass
    return 0.0


def unit_of(pid: str) -> str:
    try:
        return (PROC / pid / "cgroup").read_text().strip().rsplit("/", 1)[-1] or "?"
    except OSError:
        return "?"


def rss_of(pid: str) -> int:
    try:
        for line in (PROC / pid / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def gpu_holders(own_cgroup: str | None) -> list[dict]:
    """Per-process amdgpu GTT+VRAM (deduplicated by drm-client-id), unreadable processes skipped."""
    holders = []
    for d in PROC.iterdir():
        if not d.name.isdigit():
            continue
        clients: dict[str, int] = {}
        try:
            for fd in (d / "fdinfo").iterdir():
                try:
                    kv = dict(l.split(":", 1) for l in fd.read_text().splitlines() if ":" in l)
                except OSError:
                    continue
                if kv.get("drm-driver", "").strip() != "amdgpu":
                    continue
                kib = sum(int(kv.get(k, "0 KiB").split()[0]) for k in ("drm-memory-gtt", "drm-memory-vram"))
                clients[kv.get("drm-client-id", fd.name).strip()] = kib * 1024
        except OSError:
            continue
        gpu = sum(clients.values())
        if not gpu:
            continue
        try:
            cg = (d / "cgroup").read_text()
            comm = (d / "comm").read_text().strip()
        except OSError:
            continue
        if own_cgroup and own_cgroup in cg:
            continue
        holders.append({"pid": int(d.name), "comm": comm, "unit": unit_of(d.name), "gpu": gpu, "rss": rss_of(d.name)})
    return sorted(holders, key=lambda h: -h["gpu"])


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([*SYSTEMCTL, *args], capture_output=True, text=True)


def is_active(unit: str) -> bool:
    return systemctl("is-active", unit).stdout.strip() == "active"


def own_container_cgroup() -> str | None:
    try:
        r = subprocess.run(["podman", "inspect", "-f", "{{.Id}}", "sonoranlab-comfy"], capture_output=True, text=True, timeout=10)
        return f"libpod-{r.stdout.strip()}" if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def save_state(units: list[str]) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps({"paused": units, "pid": os.getpid(), "since": time.time()}))
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(STATE)


def container_busy() -> bool:
    """True if the SonoranLab container has a running/pending ComfyUI job, or is running but can't say (fail closed).

    The heavy work is a prompt inside the container, which outlives an interrupted host-side client; restoring a
    paused model service while it still runs would recreate the 2026-10-18 memory exhaustion."""
    podman = shlex.split(os.environ.get("SONORANLAB_PODMAN", "podman"))
    name = "sonoranlab-comfy"
    try:
        if subprocess.run([*podman, "container", "exists", name], capture_output=True, timeout=15).returncode != 0:
            return False
        r = subprocess.run([*podman, "inspect", "-f", "{{.State.Running}}", name], capture_output=True, text=True, timeout=15)
        if r.stdout.strip() != "true":
            return False
        r = subprocess.run([*podman, "exec", name, "curl", "-sf", "-m", "5", "http://127.0.0.1:8188/queue"],
                           capture_output=True, text=True, timeout=20)
        q = json.loads(r.stdout) if r.returncode == 0 else None
        return q is None or bool(q["queue_running"] or q["queue_pending"])
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return True


def wait_idle(drain_s: float) -> bool:
    """Wait up to drain_s for the container to go idle. Ctrl-C ends the wait early (answering "not idle")."""
    deadline, announced = time.monotonic() + drain_s, False
    old = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        while container_busy():
            if time.monotonic() >= deadline:
                return False
            if not announced:
                log(f"waiting up to {drain_s:.0f} s for the sonoranlab-comfy job to finish before restoring")
                announced = True
            time.sleep(2)
        return True
    except KeyboardInterrupt:
        return False
    finally:
        signal.signal(signal.SIGINT, old)


def restore(units: list[str], drain_s: float) -> bool:
    if not wait_idle(drain_s):
        log(f"WARNING: sonoranlab-comfy still has a running/pending job (or its queue is unreadable); NOT restarting "
            f"{units}. They stay stopped and recorded in {STATE}; run `python3 env/runwindow.py --restore` once it is idle.")
        return False
    ok = True
    for u in units:
        r = systemctl("start", u)
        good = r.returncode == 0 and is_active(u)
        log(f"restore {u}: {'active' if good else 'FAILED ' + r.stderr.strip()}")
        ok &= good
    if ok:
        STATE.unlink(missing_ok=True)
    return ok


def restore_from_state(drain_s: float) -> int:
    if not STATE.exists():
        log("nothing to restore")
        return 0
    try:
        units = json.loads(STATE.read_text())["paused"]
        assert isinstance(units, list) and all(isinstance(u, str) for u in units)
    except (OSError, ValueError, KeyError, AssertionError):
        log(f"state file {STATE} is unreadable; check `systemctl --user status` of the units you allowed to pause, "
            "start them yourself, then delete the file")
        return 1
    log(f"restoring units left paused by an interrupted run: {units}")
    return 0 if restore(units, drain_s) else 1


def acquire_lock():
    """Non-blocking exclusive lock shared by runs and --restore; returns the open file (keep it) or None if held."""
    WORK.mkdir(parents=True, exist_ok=True)
    f = open(LOCK, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def norm(u: str) -> str:
    return u if "." in u else f"{u}.service"


def plan(need: int, margin: int, max_psi: float, allowed: set[str], own: str | None) -> dict:
    avail, psi = mem_available(), psi_some_avg10()
    holders = gpu_holders(own)
    big = [h for h in holders if h["gpu"] >= GIB]
    target = need + margin
    p = {"need_gib": need / GIB, "margin_gib": margin / GIB, "available_gib": round(avail / GIB, 2),
         "psi_some_avg10": psi, "holders": [{**h, "gpu_gib": round(h["gpu"] / GIB, 2)} for h in big],
         "pause": [], "ok": False, "reason": ""}
    if psi > max_psi:
        p["reason"] = f"memory pressure some avg10={psi} > {max_psi}: the box is already under pressure"
        return p
    if avail >= target:
        p["ok"], p["reason"] = True, "fits without pausing anything"
        return p
    pausable = [h for h in big if h["unit"] in allowed and h["unit"].endswith(".service")]
    # ponytail: reclaim is optimistic (mmap'd model pages may already count as MemAvailable, observed 2026-10-18 with
    # llama-server); the post-pause settle check is the real gate and restores the unit if memory doesn't free up.
    # Upgrade path: sample MemAvailable before/after a trial pause per unit and cache the measured reclaim.
    reclaim = sum(h["gpu"] + h["rss"] for h in pausable)
    units = sorted({h["unit"] for h in pausable})
    if avail + reclaim >= target:
        p.update(ok=True, pause=units, reason=f"fits after pausing approved {units}")
        return p
    others = sorted({h["unit"] for h in big if h["unit"] not in allowed})
    p["reason"] = (f"needs {target / GIB:.0f} GiB available, have {avail / GIB:.1f} GiB"
                   + (f" (+{reclaim / GIB:.1f} from approved pauses)" if reclaim else "")
                   + (f"; other GPU-memory holders: {others}. Re-run with --allow-pause <unit> for user services you "
                      "approve interrupting, or close the others yourself" if others else ""))
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--need-gib", type=float, default=52.0, help="host memory the workload needs (Qwen Edit FP8 ≈ 52)")
    ap.add_argument("--margin-gib", type=float, default=8.0, help="headroom kept free for the desktop / oomd")
    ap.add_argument("--max-psi", type=float, default=10.0, help="refuse if memory PSI some avg10 is above this")
    ap.add_argument("--allow-pause", action="append", default=[], metavar="UNIT", help="user unit you approve pausing")
    ap.add_argument("--settle-s", type=float, default=30.0, help="wait this long for memory to free after pausing")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", action="store_true", help="restore units left paused by a killed run, then exit")
    ap.add_argument("--drain-s", type=float, default=1800.0,
                    help="before restoring, wait this long for the container's ComfyUI job to finish (else keep paused)")
    ap.add_argument("--own-cgroup", default=None, help="cgroup substring of our own container (auto-detected)")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd and not a.dry_run and not a.restore:
        ap.error("no command given (put it after --)")

    # Every path that can touch service state, including --restore, holds the same exclusive lock. A live window
    # owns its paused units until it exits; the kernel drops the lock when that process dies, even on SIGKILL.
    lock = acquire_lock()
    if lock is None:
        log("another run window is active; refusing (service state left untouched)")
        return 4
    if a.restore:
        return restore_from_state(a.drain_s)
    if STATE.exists() and restore_from_state(a.drain_s) != 0:
        log("could not restore units from a previous interrupted run; refusing")
        return 3

    own = a.own_cgroup if a.own_cgroup is not None else own_container_cgroup()
    p = plan(int(a.need_gib * GIB), int(a.margin_gib * GIB), a.max_psi, {norm(u) for u in a.allow_pause}, own)
    print(json.dumps({"preflight": p}, default=str), file=sys.stderr, flush=True)
    if not p["ok"]:
        log(f"REFUSED: {p['reason']}")
        return 3
    if a.dry_run:
        log(f"dry run OK: {p['reason']}")
        return 0

    paused: list[str] = []
    state = {"rc": 1, "child": None}

    def forward(signum, _frame):
        state["rc"] = 128 + signum
        child = state["child"]
        if child and child.poll() is None:
            child.send_signal(signum)  # let the command clean up; we restore after it exits
        else:
            raise KeyboardInterrupt

    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, forward)
    try:
        state["rc"] = run_window(p["pause"], paused, cmd, a, state)
    except KeyboardInterrupt:
        state["rc"] = state["rc"] if state["rc"] >= 128 else 130
    finally:
        for s in (signal.SIGTERM, signal.SIGHUP):   # a second signal must not abort the restore half-way
            signal.signal(s, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)   # wait_idle() re-enables Ctrl-C just for its wait
        child = state["child"]
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(30)
            except subprocess.TimeoutExpired:
                child.kill()
        restored = restore(paused, a.drain_s) if paused else True
    if not restored:
        log("WARNING: some units were not restored; run `python3 env/runwindow.py --restore`")
        return state["rc"] or 1
    return state["rc"]


def run_window(pause: list[str], paused: list[str], cmd: list[str], a: argparse.Namespace, state: dict) -> int:
    for u in pause:
        if is_active(u):
            # Recorded (state file and in-memory) before stopping, so a signal or SIGKILL mid-stop still restores it.
            paused.append(u)
            save_state(paused)
            r = systemctl("stop", u)
            if r.returncode != 0:
                log(f"could not stop {u}: {r.stderr.strip()}")
                return 3
            log(f"paused {u} (was active)")
    target = int((a.need_gib + a.margin_gib) * GIB)
    deadline = time.monotonic() + a.settle_s
    while mem_available() < target:
        if time.monotonic() > deadline:
            log(f"REFUSED: still only {mem_available() / GIB:.1f} GiB available after pausing {paused}")
            return 3
        time.sleep(1)
    # Marks the child as guarded, so env/smoke.sh runs directly instead of re-entering a window.
    state["child"] = subprocess.Popen(cmd, env={**os.environ, "SONORANLAB_RUNWINDOW": str(os.getpid())})
    state["child"].wait()
    return state["rc"] if state["rc"] >= 128 else state["child"].returncode

if __name__ == "__main__":
    sys.exit(main())
