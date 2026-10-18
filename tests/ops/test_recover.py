"""Regression test: env/recover.sh leaves service state to env/runwindow.py and never replaces a busy container."""

import re
import subprocess
from pathlib import Path

RECOVER = Path(__file__).resolve().parents[2] / "env" / "recover.sh"


def test_recover_never_manages_services_directly():
    src = RECOVER.read_text()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert not re.search(r"\bsystemctl\b", code), "recover.sh must leave service state to env/runwindow.py"
    assert re.search(r"runwindow\.py --restore --drain-s \d+", code)  # fail fast behind an orphaned job
    assert re.search(r"runwindow\.py --need-gib \d+", code)
    assert "comfy.sh up --replace" in code
    assert subprocess.run(["bash", "-n", str(RECOVER)]).returncode == 0


def test_recover_step_log_lines_are_not_redirected_away():
    # `step NAME cmd >/dev/null` would also swallow step's own "== NAME" / "rc=" lines and the failure verdict.
    code = [l for l in RECOVER.read_text().splitlines() if l.startswith("step ")]
    assert code and not [l for l in code if re.search(r">\s*/dev/null\s*(\|\||$)", l)]
    assert "RECOVERY FAIL at" in RECOVER.read_text()
