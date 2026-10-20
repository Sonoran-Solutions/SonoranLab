"""DYL-101: the bounded retry around Godot 4.7.2's intermittent headless `Parameter "singleton" is null` crash.
Uses a fake `godot` on PATH (a script driven by a per-call plan), so it needs neither Godot nor a GPU."""

import json
import os
import stat
import sys

import numpy as np
import pytest

from sonoranlab import pipeline
from sonoranlab.pipeline import check_in_godot, run_godot

SIG = 'ERROR: Parameter "singleton" is null.\n   at: is_cmdline_mode (editor/editor_node.cpp:6618)\n'
TRANSIENT = {"stderr": SIG, "signal": 6}
OK = {"stdout": "imported\n"}
ROWS = ["north", "north-west", "west", "south-west", "south", "south-east", "east", "north-east"]
CLIPS = [{"name": "run", "start": 1, "frames": 2, "fps": 10, "loop": True}]

FAKE = """\
#!{python}
import json, os, resource, signal, sys
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
plan = json.load(open({plan!r}))
args = sys.argv[1:]
if "--version" in args:
    print("4.7.2.fake"); sys.exit(0)
step = "check" if "--script" in args else "import"
with open({log!r}, "a") as f:
    f.write(step + "\\n")
n = sum(1 for ln in open({log!r}) if ln.strip() == step) - 1
beh = plan[step][min(n, len(plan[step]) - 1)] if step in plan else {{}}
if step == "check" and not beh:
    beh = {{"stdout": json.dumps({{"pass": True, "animations_checked": 8}}) + "\\n"}}
sys.stdout.write(beh.get("stdout", "")); sys.stderr.write(beh.get("stderr", ""))
sys.stdout.flush(); sys.stderr.flush()
if beh.get("signal"):
    os.kill(os.getpid(), beh["signal"])
sys.exit(beh.get("rc", 0))
"""


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """fake(import=[behaviour per call], check=[...]) -> path of the call log; the last behaviour repeats."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(pipeline, "GODOT_BACKOFF", (0.0, 0.0))

    def make(**plan):
        (tmp_path / "plan.json").write_text(json.dumps(plan))
        log = tmp_path / "calls.log"
        log.write_text("")
        exe = bindir / "godot"
        exe.write_text(FAKE.format(python=sys.executable, plan=str(tmp_path / "plan.json"), log=str(log)))
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        return log

    return make


def run(tmp_path, attempts=None):
    return run_godot("godot", ["--headless", "--import"], cwd=tmp_path, step="import", attempts=attempts)


def test_transient_then_success_is_retried_and_recorded(tmp_path, fake):
    log = fake(**{"import": [TRANSIENT, TRANSIENT, OK]})
    attempts: list[dict] = []
    p = run(tmp_path, attempts)
    assert p.returncode == 0 and len(log.read_text().split()) == 3
    assert [a["returncode"] for a in attempts] == [-6, -6, 0]
    assert [a["transient_signature"] for a in attempts] == [True, True, False]
    assert all(a["step"] == "import" and a["seconds"] >= 0 for a in attempts)


def test_exit_134_counts_as_abort(tmp_path, fake):
    fake(**{"import": [{"stderr": SIG, "rc": 134}, OK]})
    attempts: list[dict] = []
    assert run(tmp_path, attempts).returncode == 0 and len(attempts) == 2


def test_transient_three_times_fails_after_three_attempts(tmp_path, fake):
    log = fake(**{"import": [TRANSIENT]})
    attempts: list[dict] = []
    p = run(tmp_path, attempts)
    assert p.returncode == -6 and len(log.read_text().split()) == 3
    assert len(attempts) == 3 and all(a["transient_signature"] for a in attempts)


@pytest.mark.parametrize(
    "beh",
    [
        {"stderr": "ERROR: Failed to load resource 'res://x.tres'.\n", "rc": 1},  # real import error
        {"stderr": SIG, "rc": 1},  # right text, wrong exit status
        {"stderr": "ERROR: something else\n", "signal": 6},  # right status, wrong text
        {"stderr": SIG + "ERROR: Failed to import res://toy/toy.png\n", "signal": 6},  # signature + content error
        {"stdout": "SCRIPT ERROR: Parse Error: x\n", "stderr": SIG, "signal": 6},
        {"stdout": '{"pass": false, "failures": ["speed"]}\n', "stderr": SIG, "signal": 6},
    ],
)
def test_anything_else_fails_at_once_with_one_attempt(tmp_path, fake, beh):
    log = fake(**{"import": [beh, OK]})
    attempts: list[dict] = []
    p = run(tmp_path, attempts)
    assert p.returncode != 0 and len(log.read_text().split()) == 1
    assert len(attempts) == 1 and attempts[0]["transient_signature"] is False


# --- through check_in_godot -----------------------------------------------------------------------------------------


def toy_sheet() -> np.ndarray:
    sheet = np.zeros((len(ROWS) * 16, 3 * 16, 4), np.uint8)
    sheet[::16, 16::16] = (200, 30, 30, 255)
    return sheet


def check():
    return check_in_godot(toy_sheet(), character="toy", cell=(16, 16), rows=ROWS, clips=CLIPS)


def test_check_in_godot_recovers_and_reports_attempts(fake):
    fake(**{"import": [TRANSIENT, OK]})
    gc = check()
    assert gc["pass"] and gc["import_exit"] == 0 and gc["check_exit"] == 0
    got = [(a["step"], a["returncode"], a["transient_signature"]) for a in gc["godot_attempts"]]
    assert got == [("version", 0, False), ("import", -6, True), ("import", 0, False), ("check", 0, False)]


def test_check_in_godot_exhausted_retries_is_a_failure(fake):
    fake(**{"import": [TRANSIENT]})
    gc = check()
    assert not gc["pass"] and gc["import_exit"] == -6
    assert [a["step"] for a in gc["godot_attempts"]].count("import") == 3


def test_check_in_godot_real_failure_is_not_retried(fake):
    fake(**{"import": [{"stderr": "ERROR: Failed to load\n", "rc": 1}]})
    gc = check()
    assert not gc["pass"] and [a["step"] for a in gc["godot_attempts"]].count("import") == 1


def test_recovered_import_with_failing_check_still_fails(fake):
    fake(**{"import": [TRANSIENT, OK], "check": [{"stdout": json.dumps({"pass": False}) + "\n", "rc": 1}]})
    gc = check()
    assert not gc["pass"] and gc["check_exit"] == 1 and [a["step"] for a in gc["godot_attempts"]].count("check") == 1
