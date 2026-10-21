"""Importable subprocess fakes. All coordination is file-backed; no GPU or service required."""

import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

from sonoranlab.api.paths import state_root
from sonoranlab.jobs import JobFailure, store


def fake_root():
    path = state_root() / "fake"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write(path, value):
    store.atomic(path, value)


def log(value):
    root = fake_root()
    with store.lock(root / "log.lock"), (root / "events.jsonl").open("a") as stream:
        stream.write(json.dumps({**value, "at": time.monotonic()}) + "\n")


class Prober:
    def status(self, prompt_id):
        root = fake_root()
        if (root / "probe-error").exists():
            raise OSError("fake probe failure")
        if (root / "probe-hold").exists():
            (root / "probe-ready").touch()
            wait_file(root / "probe-release")
        path = root / f"{prompt_id}.json"
        return json.loads(path.read_text())["state"] if path.exists() else "unknown"

    def cancel(self, prompt_id):
        root = fake_root()
        log({"event": "cancel", "prompt_id": prompt_id})
        if (root / "still-running").exists():
            return False
        write(root / f"{prompt_id}.json", {"state": "unknown"})
        return True


def wait_file(path, ctx=None):
    deadline = time.monotonic() + 25
    while not path.exists():
        if ctx is not None:
            ctx.check_cancelled()
        if time.monotonic() > deadline:
            raise TimeoutError(str(path))
        time.sleep(0.01)


class Fake:
    heavy_cpu = False

    def __init__(self, kind, gpu=False):
        self.kind, self.gpu = kind, gpu

    def validate(self, params):
        if params.get("invalid"):
            raise ValueError("invalid fake request")
        normalized = {"project": "test", "character": "actor", "anim": "walk", **params}
        hashes = {}
        if "input" in params:
            hashes = {"input": hashlib.sha256(Path(params["input"]).read_bytes()).hexdigest()}
        return normalized, hashes

    def need_gib(self, params):
        return 40 if self.gpu else None

    def stage(self, job):
        root = store.output(job)
        return {"results": len(list(root.rglob("result.json"))), "ready": (root / "ready").exists()}

    def run(self, job, ctx):
        root, params = ctx.output_dir, job["params"]
        log({"event": "start", "job_id": job["job_id"]})
        ctx.set_stage("generate" if self.gpu else "prepare")
        (root / "ready").touch()
        prompt_id = None
        try:
            if params.get("boundary") == "before_submit":
                checkpoint(root, "before_submit", params, ctx)
            if self.gpu:
                attempt = root / "gen"
                attempt.mkdir()
                prompt_id = str(uuid.uuid4())
                write(attempt / "submission.json", {"prompt_id": prompt_id})
                if params.get("boundary") == "persisted_before_post":
                    checkpoint(root, "persisted_before_post", params, ctx)
                write(fake_root() / f"{prompt_id}.json", {"state": "running"})
                log({"event": "submit", "job_id": job["job_id"], "prompt_id": prompt_id})
                (root / "submitted").touch()
                if params.get("boundary") == "after_submission":
                    checkpoint(root, "after_submission", params, ctx)
            if params.get("boundary") == "processing":
                ctx.set_stage("select_and_process")
                checkpoint(root, "processing", params, ctx)
            if params.get("boundary") == "export":
                ctx.set_stage("validate_export")
                checkpoint(root, "export", params, ctx)
            if self.kind != "fast" and not params.get("instant"):
                wait_file(root / "release", ctx)
            if params.get("guard_exit"):
                ctx.run([sys.executable, "-c", f"raise SystemExit({params['guard_exit']})"])
            if params.get("command"):
                ctx.run(params["command"], check=False)
            if params.get("child"):
                script = (
                    "import signal,time; from pathlib import Path; "
                    f"p=Path({str(root / 'child-ready')!r}); "
                    f"signal.signal(signal.SIGTERM,lambda *_:(Path({str(root / 'child-stopped')!r}).touch(),exit(0))); "
                    "p.touch(); time.sleep(25)"
                )
                ctx.run([sys.executable, "-c", script], check=False)
                if params.get("more_children"):
                    ctx.run(
                        [
                            sys.executable,
                            "-c",
                            f"from pathlib import Path; Path({str(root / 'extra-child')!r}).touch()",
                        ],
                        check=False,
                    )
            if params.get("log_bytes"):
                os.write(2, b"x" * params["log_bytes"])
            if unfinished := params.get("unfinished"):
                if unfinished in {"cancel_unconfirmed", "submit_unknown"}:
                    write(root / "gen/result.json", {"error": {"code": unfinished}})
                if unfinished == "failure":
                    raise JobFailure("fake_failure", "generate", "executor failed", True)
                if unfinished == "raise":
                    raise RuntimeError("executor crashed")
                return {"complete": True, "partial": True}
            if prompt_id:
                write(fake_root() / f"{prompt_id}.json", {"state": "done"})
                write(root / "gen/result.json", {"status": "ok"})
            (root / "artifact.txt").write_text("fake artifact")
            return {"complete": True}
        finally:
            log({"event": "end", "job_id": job["job_id"]})


def checkpoint(root, name, params, ctx):
    (root / name).touch()
    if params.get("crash"):
        os._exit(81)
    wait_file(root / "release-boundary", ctx)


heavy = Fake("heavy")
heavy.heavy_cpu = True
EXECUTORS = {"fast": Fake("fast"), "slow": Fake("slow"), "fake-gpu": Fake("fake-gpu", True), "heavy": heavy}
