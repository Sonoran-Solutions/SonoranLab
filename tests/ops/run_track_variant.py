"""Test-only launcher for benchmark/tools/run_track.py; production code is untouched.

RT_STAGE_DELAY_S: hold the workflow.json write (after reservation and staging) for that many seconds.
RT_UNSAFE=1: make reservation non-exclusive (a deliberately broken runner, to prove the tests can fail).
"""

import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "benchmark/tools"))
import run_track

_write_text, _mkdir = pathlib.Path.write_text, pathlib.Path.mkdir


def write_text(self, *a, **kw):
    if self.name == "workflow.json":
        time.sleep(float(os.environ.get("RT_STAGE_DELAY_S", "0")))
    return _write_text(self, *a, **kw)


pathlib.Path.write_text = write_text
if os.environ.get("RT_UNSAFE"):
    pathlib.Path.mkdir = lambda self, mode=0o777, parents=False, exist_ok=False: _mkdir(self, mode, parents, True)

    def unsafe_reserve(out, job, argv):
        out.mkdir(parents=True)
        return {"job": job, "pid": os.getpid(), "host": "h", "created": "t", "argv": argv}

    run_track.reserve = unsafe_reserve
sys.exit(run_track.main())
