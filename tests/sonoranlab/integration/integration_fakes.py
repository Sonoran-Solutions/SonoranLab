"""Use real admission/pipelines; replace only execution at the existing executor hook."""

import os
import subprocess
import sys
from pathlib import Path

from job_fakes import Fake, Prober, log, wait_file  # noqa: F401

from sonoranlab.jobs import store
from sonoranlab.jobs.kinds.deterministic import EXECUTORS as DETERMINISTIC
from sonoranlab.jobs.kinds.generation import EXECUTORS as GENERATION


class Gated:
    def __init__(self, original, *, gpu=False):
        self.original = original
        self.fake = Fake("slow", gpu=gpu)
        self.gpu, self.heavy_cpu = gpu, not gpu

    def __getattr__(self, name):
        return getattr(self.original, name)

    def stage(self, job):
        return self.fake.stage(job)

    def run(self, job, ctx):
        # Existing fake records submission IDs, timestamps and responds to scoped cancellation.
        return self.fake.run(job, ctx)


class Pose:
    def __init__(self, original):
        self.original = original

    def __getattr__(self, name):
        return getattr(self.original, name)

    def run(self, job, ctx):
        def run(argv, **kwargs):
            ctx.check_cancelled()
            log({"event": "argv", "job_id": job["job_id"], "argv": argv})
            request = store.load(Path(argv[argv.index("--request") + 1]))
            out = Path(request["out_dir"])
            out.mkdir()
            store.atomic(out / "result.json", {"status": "ok", "request": request})
            (out / "pose.png").write_bytes(b"synthetic pose")
            return subprocess.CompletedProcess(argv, 0)

        ctx.run = run
        return self.original.run(job, ctx)


class Noisy(Gated):
    def run(self, job, ctx):
        (ctx.output_dir / "ready").touch()
        wait_file(ctx.output_dir / "release", ctx)
        ctx.run([sys.executable, "-c", "import os; os.write(2,b'x'*(3*1024*1024))"])
        return {"complete": True}


mode = os.environ.get("INTEGRATION_MODE", "gate")
EXECUTORS = {}
if mode in {"gate", "noisy"}:
    EXECUTORS["process"] = (Noisy if mode == "noisy" else Gated)(DETERMINISTIC["process"])
    EXECUTORS["animate"] = Gated(GENERATION["animate"], gpu=True)
elif mode == "pose":
    EXECUTORS = {"pose_frame": Pose(GENERATION["pose_frame"])}
