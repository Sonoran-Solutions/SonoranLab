"""Track E adapter with a fake ComfyUI client: CPU only, no server, no ~/sonoranlab-work."""

import hashlib
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from PIL import Image

from sonoranlab import cli
from sonoranlab.comfy import provenance
from sonoranlab.comfy.adapters import track_e
from sonoranlab.comfy.adapters.base import DEFAULT_BACKGROUND, GenerateRequest, RawResult
from sonoranlab.comfy.adapters.track_e import RUN_PROMPTS, TrackE, alpha_fringe_stats, guarded_argv
from sonoranlab.comfy.client import Workflow, graph_sha256
from sonoranlab.comfy.guard import pending_submissions

from .birefnet_fixture import work_untouched, write_birefnet

DIRS = ["north", "north-east", "east", "south-east", "south"]


def write_still(path: Path, cell: int = 16) -> Path:
    im = Image.new("RGBA", (cell, cell), (0, 0, 0, 0))
    im.paste((180, 40, 40, 255), (4, 4, cell - 4, cell - 4))
    im.save(path)
    return path


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class FakeClient:
    """Writes fake ComfyUI outputs under output_root and returns a RunRecord-shaped object."""

    def __init__(
        self,
        output_root: Path,
        *,
        raw: int | None = None,
        rgba: int | None = None,
        status="success",
        boom: BaseException | None = None,
    ) -> None:
        self.root, self.raw, self.rgba, self.status, self.boom = output_root, raw, rgba, status, boom
        self.calls: list[dict[str, Any]] = []

    def run(self, workflow, values, *, timeout_s, expect, prompt_id=None):
        assert str(uuid.UUID(prompt_id)) == prompt_id
        submissions = [
            p
            for p in self.root.parent.parent.rglob("submission.json")
            if json.loads(p.read_text())["prompt_id"] == prompt_id
        ]
        assert len(submissions) == 1 and not (submissions[0].parent / "result.json").exists()
        self.calls.append(
            {
                "workflow": workflow,
                "values": dict(values),
                "timeout_s": timeout_s,
                "expect": expect,
                "prompt_id": prompt_id,
            }
        )
        if self.boom:
            raise self.boom
        n = values["length"]
        outs = []
        for node, key, count in (("90", "raw", self.raw), ("92", "rgba", self.rgba), ("93", "preview", 1)):
            prefix = values[f"output_prefix_{key}"]
            (self.root / prefix).parent.mkdir(parents=True, exist_ok=True)
            for i in range(n if count is None else count):
                a = np.full((8, 8, 4), 128, np.uint8)
                a[..., 3] = 255
                if node == "92":  # subject on a transparent background
                    a[..., 3] = 0
                    a[2:6, 2:6] = (200, 30, 30, 255)
                if node == "90":
                    a[0, 0, 0] = i  # distinct bytes per frame
                p = self.root / f"{prefix}_{i + 1:05d}_.png"
                Image.fromarray(a).save(p)
                outs.append(
                    SimpleNamespace(
                        node=node,
                        kind="images",
                        rel=p.relative_to(self.root).as_posix(),
                        path=p,
                        sha256=sha(p),
                        bytes=p.stat().st_size,
                    )
                )
        ok = self.status == "success"
        err = None if ok else {"code": self.status, "stage": "wait", "message": "timed out", "retryable": True}
        rec = SimpleNamespace(
            prompt_id=prompt_id,
            workflow_source="wf.json",
            workflow_source_sha256="a" * 64,
            workflow_patched_sha256="b" * 64,
            params=dict(values),
            models={"m.gguf": "c" * 64},
            outputs=outs,
            status=self.status,
            timing={"exec_s": 1.5, "wall_s": 2.0},
            error=err,
        )
        rec.to_json = lambda: {"prompt_id": prompt_id, "status": self.status}
        return rec


@pytest.fixture
def env(tmp_path: Path):
    work = tmp_path / "work"
    return SimpleNamespace(
        tmp=tmp_path,
        work=work,
        still=write_still(tmp_path / "still.png"),
        client=FakeClient(work / "output"),
        models_tsv=write_birefnet(work),
    )


def request(env, **kw) -> GenerateRequest:
    base = dict(
        track="E",
        character="samurai",
        anim="run",
        direction="east",
        source_image=env.still,
        prompt="",
        seed=7,
        out_dir=env.tmp / "attempts" / "a1",
    )
    return GenerateRequest(**{**base, **kw})


def adapter(env) -> TrackE:
    return TrackE(env.client, work_root=env.work, workflow=object(), timeout_s=99, models_tsv=env.models_tsv)


def test_success_layout_and_values(env) -> None:
    req = request(env)
    res = adapter(env).generate(req)
    out = req.out_dir
    assert res.status == "ok" and res.error is None
    assert len(res.raw_frames) == len(res.frames) == 33
    assert [p.name for p in res.frames] == sorted(p.name for p in res.frames)
    assert {p.name for p in out.iterdir()} == {
        "source",
        "clip_rgb",
        "clip_rgba",
        "preview",
        "request.json",
        "submission.json",
        "run.json",
        "result.json",
    }
    assert len(list((out / "preview").iterdir())) == 1
    assert res.outputs[f"clip_rgba/{res.frames[0].name}"] == sha(res.frames[0])
    assert len(res.outputs) == 67
    assert RawResult.from_json(json.loads((out / "result.json").read_text())) == res
    assert json.loads((out / "request.json").read_text()) == req.to_json()
    submission = json.loads((out / "submission.json").read_text())
    assert submission == {
        "schema": "sonoranlab.submission/1",
        "prompt_id": res.prompt_id,
        "client_id": None,
        "base_url": None,
        "workflow_source": str(track_e.DEFAULT_WORKFLOW),
        "workflow_patched_sha256": None,
        "state": "submitting",
        "written_at": submission["written_at"],
    }
    assert datetime.fromisoformat(submission["written_at"]).utcoffset().total_seconds() == 0
    assert res.models == {"m.gguf": "c" * 64} and res.timing["wall_s"] == 2.0
    assert json.loads((out / "run.json").read_text())["prompt_id"] == res.prompt_id
    assert pending_submissions(out.parent) == []

    (call,) = env.client.calls
    assert call["prompt_id"] == res.prompt_id
    v = call["values"]
    attempt = Path(v["start_image"]).parent.name
    assert v["start_image"] == f"sonoranlab/{attempt}/start_image.png"
    assert attempt.startswith("a1-")
    staged = env.work / "input" / attempt / "start_image.png"
    assert sha(staged) == sha(out / "source/start_image.png") == res.inputs["start_image"]
    assert v["output_prefix_raw"] == f"p3/{attempt}/raw" and v["output_prefix_preview"] == f"p3/{attempt}/preview"
    assert (v["width"], v["height"], v["length"], v["steps"], v["split_step"], v["cfg"]) == (48, 48, 33, 4, 2, 1)
    assert (v["lora_strength"], v["fps"], v["seed"]) == (1.0, 16, 7)
    assert call["expect"] == {"90": 33, "92": 33} and call["timeout_s"] == 99
    with Image.open(staged) as im:
        assert im.size == (48, 48) and im.getpixel((0, 0)) == (128, 128, 128)  # 16 * 3 = 48, grey default
    assert res.checks["source"]["canvas"] == 48 and res.checks["source"]["background"] == DEFAULT_BACKGROUND


def test_canvas_for_samurai_cell(env) -> None:
    req = request(env, source_image=write_still(env.tmp / "samurai.png", 184))
    adapter(env).generate(req)
    assert (env.client.calls[0]["values"]["width"], env.client.calls[0]["values"]["height"]) == (560, 560)


def test_start_scale_places_the_still_smaller_on_the_same_canvas(env) -> None:
    """Fall clips (profile start_scale): the gun's 160 px cell keeps the 480 px canvas, so WanImageToVideo gets the same
    width/height (same latent tokens, same admitted memory); the still sits at x2, centred, with 80 px of room each
    side. Recorded in request.json and checks.source; a default request carries neither key."""
    still = write_still(env.tmp / "gun.png", 160)
    default = adapter(env).generate(request(env, source_image=still, out_dir=env.tmp / "attempts" / "x3"))
    fall = adapter(env).generate(
        request(env, source_image=still, anim="death", prompt="falls", start_scale=2, out_dir=env.tmp / "attempts/x2")
    )
    assert default.status == fall.status == "ok"
    v3, v2 = (c["values"] for c in env.client.calls)
    assert (v3["width"], v3["height"]) == (v2["width"], v2["height"]) == (480, 480)
    assert {
        k: v for k, v in v3.items() if k not in ("start_image", "prompt") and not k.startswith("output_prefix")
    } == {k: v for k, v in v2.items() if k not in ("start_image", "prompt") and not k.startswith("output_prefix")}
    assert "start_scale" not in json.loads((default.request.out_dir / "request.json").read_text())
    assert "start_scale" not in default.checks["source"] and "placement" not in default.checks["source"]
    assert json.loads((fall.request.out_dir / "request.json").read_text())["start_scale"] == 2
    assert (fall.checks["source"]["start_scale"], fall.checks["source"]["placement"]) == (2, [80, 80])
    assert RawResult.from_json(json.loads((fall.request.out_dir / "result.json").read_text())).request.start_scale == 2
    with Image.open(fall.request.out_dir / "source/start_image.png") as im:
        px = np.asarray(im)
    body = np.argwhere((px != 128).any(-1))  # write_still: opaque square at still (4..155) -> x2 at 80+8 .. 80+311
    assert (body.min(0).tolist(), body.max(0).tolist()) == ([88, 88], [391, 391])
    assert (px[:, :88] == 128).all() and (px[:, 392:] == 128).all()  # background room left and right


@pytest.mark.parametrize("kw", [{"start_scale": 4}, {"start_scale": 0}, {"start_scale": True}])
def test_start_scale_refusals(env, kw) -> None:
    req = request(env, **kw)
    with pytest.raises(ValueError, match="start_scale"):
        adapter(env).generate(req)
    assert not req.out_dir.exists() and env.client.calls == []


def test_start_scale_refuses_a_canvas_source(env) -> None:
    posed = env.tmp / "posed.png"
    Image.new("RGB", (64, 64), (10, 20, 30)).save(posed)
    with pytest.raises(ValueError, match="already a canvas"):
        adapter(env).generate(request(env, source_image=posed, start_scale=2))


@pytest.mark.parametrize("direction", DIRS)
def test_default_prompt_per_direction(env, direction: str) -> None:
    adapter(env).generate(request(env, direction=direction))
    prompt = env.client.calls[0]["values"]["prompt"]
    assert prompt == RUN_PROMPTS[direction]
    assert "grey background" in prompt and "magenta" not in prompt
    assert len(set(RUN_PROMPTS.values())) == 5


def test_prompt_override_and_settings_override(env) -> None:
    res = adapter(env).generate(
        request(env, prompt="  custom  ", settings={"length": 17, "steps": 6, "negative_prompt": "blur"})
    )
    v = env.client.calls[0]["values"]
    assert v["prompt"] == "custom" and (v["length"], v["steps"], v["negative_prompt"]) == (17, 6, "blur")
    assert env.client.calls[0]["expect"] == {"90": 17, "92": 17}
    assert res.status == "ok" and len(res.frames) == 17 and res.request.settings["steps"] == 6


def test_canvas_size_rgb_source_used_as_is(env) -> None:
    posed = env.tmp / "posed.png"
    Image.new("RGB", (64, 64), (10, 20, 30)).save(posed)
    res = adapter(env).generate(request(env, source_image=posed))
    assert res.status == "ok" and res.checks["source"]["kind"] == "canvas"
    assert sha(res.request.out_dir / "source/start_image.png") == sha(posed) == res.inputs["start_image"]
    assert env.client.calls[0]["values"]["width"] == 64


def test_client_failure_keeps_partial_outputs(env) -> None:
    env.client = FakeClient(env.work / "output", raw=10, rgba=5, status="timeout")
    req = request(env)
    res = adapter(env).generate(req)
    assert res.status == "failed" and res.error == {
        "code": "timeout",
        "stage": "wait",
        "message": "timed out",
        "retryable": True,
    }
    assert len(res.raw_frames) == 10 and len(res.frames) == 5
    assert all(p.exists() for p in res.raw_frames + res.frames)
    assert len(list((req.out_dir / "clip_rgb").iterdir())) == 10
    assert (req.out_dir / "run.json").exists()
    assert RawResult.from_json(json.loads((req.out_dir / "result.json").read_text())).status == "failed"


def test_success_status_but_short_clip_is_failed(env) -> None:
    env.client = FakeClient(env.work / "output", rgba=30)
    res = adapter(env).generate(request(env))
    assert res.status == "failed" and res.error["code"] == "missing_frames" and res.error["retryable"] is True


def test_client_exception_is_a_failed_result(env) -> None:
    env.client = FakeClient(env.work / "output", boom=RuntimeError("socket gone"))
    req = request(env)
    res = adapter(env).generate(req)
    assert res.status == "failed" and res.error["code"] == "adapter_exception" and "socket gone" in res.error["message"]
    assert (req.out_dir / "request.json").exists() and (req.out_dir / "result.json").exists()
    assert res.prompt_id == json.loads((req.out_dir / "submission.json").read_text())["prompt_id"]


def test_submission_fsynced_before_client_with_workflow_hash(env, monkeypatch) -> None:
    req = request(env)
    workflow = Workflow.load(track_e.DEFAULT_WORKFLOW)
    env.client.client_id = str(uuid.uuid4())
    env.client.base_url = "http://127.0.0.1:8188"
    synced = []
    fsync = os.fsync

    def sync(fd: int) -> None:
        submission = json.loads((req.out_dir / "submission.json").read_text())
        assert os.fstat(fd).st_size > 0 and not env.client.calls
        fsync(fd)
        synced.append(submission)

    monkeypatch.setattr(track_e.os, "fsync", sync)
    run = env.client.run

    def checked_run(*a, **kw):
        assert len(synced) == 1 and synced[0]["prompt_id"] == kw["prompt_id"]
        return run(*a, **kw)

    monkeypatch.setattr(env.client, "run", checked_run)
    res = TrackE(env.client, work_root=env.work, workflow=workflow, models_tsv=env.models_tsv).generate(req)
    (submission,) = synced
    assert res.status == "ok"
    assert submission["workflow_patched_sha256"] == graph_sha256(workflow.patch(env.client.calls[0]["values"]))
    assert submission["workflow_source"] == str(workflow.source)
    assert submission["client_id"] == env.client.client_id and submission["base_url"] == env.client.base_url


def test_submission_sync_failure_never_submits(env, monkeypatch) -> None:
    def boom(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(track_e.os, "fsync", boom)
    res = adapter(env).generate(request(env))
    assert res.status == "failed" and "disk full" in res.error["message"] and not env.client.calls


@pytest.mark.parametrize("boom", [SystemExit(9), KeyboardInterrupt()])
def test_interrupted_submission_is_recoverable(env, boom) -> None:
    env.client = FakeClient(env.work / "output", boom=boom)
    req = request(env)
    with pytest.raises(type(boom)):
        adapter(env).generate(req)
    assert not (req.out_dir / "result.json").exists()
    submission = json.loads((req.out_dir / "submission.json").read_text())
    assert submission["prompt_id"] == env.client.calls[0]["prompt_id"]
    assert pending_submissions(req.out_dir.parent) == [{**submission, "out_dir": str(req.out_dir)}]
    (req.out_dir / "result.json").write_text("{}")
    assert pending_submissions(req.out_dir.parent) == []


def test_staging_error_is_a_failed_result(env, monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(track_e.shutil, "copyfile", boom)
    res = adapter(env).generate(request(env))
    assert res.status == "failed" and res.error["stage"] == "stage" and env.client.calls == []


@pytest.mark.parametrize(
    "kw",
    [
        {"track": "C"},
        {"direction": "west"},
        {"direction": "up"},
        {"source_image": Path("nope.png")},
        {"settings": {"length": 32}},
        {"settings": {"bogus": 1}},
        {"settings": {"seed": 1}},
        {"settings": {"output_prefix_raw": "x"}},
        {"background": "grey"},
        {"seed": -1},
        {"control_image": Path("pose.png")},
        {"anim": "attack"},
        {"workflow": "/etc/passwd"},
    ],
)
def test_malformed_request_writes_nothing(env, kw) -> None:
    req = request(env, **kw)
    with pytest.raises(ValueError):
        adapter(env).generate(req)
    assert not req.out_dir.exists() and work_untouched(env.work) and env.client.calls == []


def test_existing_out_dir_and_staging_refused(env) -> None:
    req = request(env)
    req.out_dir.mkdir(parents=True)
    with pytest.raises(ValueError, match="exists"):
        adapter(env).generate(req)
    assert list(req.out_dir.iterdir()) == []
    req2 = request(env, out_dir=env.tmp / "b")
    (env.work / "input").mkdir(parents=True)
    res = adapter(env).generate(req2)  # first use of the staging dir succeeds ...
    assert res.status == "ok"
    req3 = request(env, out_dir=env.tmp / "b2")
    (env.work / "input" / f"b2-{hashlib.sha256(str((env.tmp / 'b2').resolve()).encode()).hexdigest()[:10]}").mkdir()
    with pytest.raises(ValueError, match="staging"):
        adapter(env).generate(req3)  # ... a leftover one is never reused


def test_alpha_fringe_counts(tmp_path: Path) -> None:
    a = np.zeros((12, 12, 4), np.uint8)
    a[2:10, 2:10] = (200, 30, 30, 255)  # 8x8 opaque body
    a[2:10, 2] = (130, 130, 130, 255)  # left column: grey edge pixels (8)
    a[2, 5] = (128, 128, 128, 255)  # top edge pixel, exact grey
    a[9, 9] = (50, 50, 50, 255)  # bottom-right corner: edge, but dark -> not fringe
    a[6, 6] = (128, 128, 128, 255)  # interior grey: not an edge
    a[0, 0] = (90, 90, 90, 128)  # a semi-transparent pixel
    a[0, 1] = (90, 90, 90, 1)
    Image.fromarray(a).save(tmp_path / "f0.png")
    Image.fromarray(np.zeros((12, 12, 4), np.uint8)).save(tmp_path / "f1.png")
    st = alpha_fringe_stats([tmp_path / "f0.png", tmp_path / "f1.png"], "#808080")
    f0, f1 = st["frames"]
    assert f0["semi_transparent"] == 2
    assert f0["edge"] == 29  # ring of an 8x8 block + the a=128 island at (0, 0), which survives the 128 threshold
    assert f0["grey_fringe"] == 9  # 8 in the left column (the corner pixels included) + the top pixel
    assert f0["empty"] is False and f1["empty"] is True and f1["edge"] == 0
    assert st["empty_frames"] == ["f1.png"] and st["totals"]["grey_fringe"] == 9


def test_alpha_fringe_on_soft_alpha_edge(tmp_path: Path) -> None:
    # BiRefNet edges are soft: no a=255 pixel touches a=0, but the a>=128 ring is what the pipeline keeps.
    a = np.zeros((10, 10, 4), np.uint8)
    a[1:9, 1:9] = (128, 128, 128, 40)  # faint outer halo, dropped by the threshold
    a[2:8, 2:8] = (126, 130, 129, 200)  # kept ring: background-coloured -> fringe
    a[3:7, 3:7] = (200, 30, 30, 255)
    Image.fromarray(a).save(tmp_path / "f.png")
    f = alpha_fringe_stats([tmp_path / "f.png"], "#808080")["frames"][0]
    assert f["edge"] == 20 and f["grey_fringe"] == 20
    assert alpha_fringe_stats([tmp_path / "f.png"], "#808080", threshold=255)["frames"][0]["grey_fringe"] == 0


def test_guarded_argv() -> None:
    argv = guarded_argv(["sonoranlab", "generate"], allow_pause=("llama-gemory",))
    assert argv[0] == "python3" and argv[1].endswith("env/runwindow.py") and Path(argv[1]).is_file()
    assert argv[2:] == ["--need-gib", "40", "--allow-pause", "llama-gemory", "--", "sonoranlab", "generate"]
    assert "--gtt-credit" not in guarded_argv(["x"], 60)
    assert guarded_argv(["x"], 60)[2:5] == ["--need-gib", "60", "--"]


# -- CLI ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def cli_env(env, monkeypatch, lease_window):
    lease_window(env.work, need_gib=60)  # also admits the malformed Track C request checked below

    def unexpected_exec(*a):
        pytest.fail("CLI test must remain inside the fake guard")

    monkeypatch.setattr(cli, "_exec", unexpected_exec)
    monkeypatch.setenv("SONORANLAB_WORK", str(env.work))
    monkeypatch.setattr(track_e, "load_workflow", lambda p: object())
    monkeypatch.setattr(provenance, "MODELS_TSV", env.models_tsv)
    made = {}

    def make(base_url, output_root, exec_container=None):
        made.update(base_url=base_url, output_root=output_root, exec_container=exec_container)
        return FakeClient(output_root, status=made.get("status", "success"))

    monkeypatch.setattr(track_e, "make_client", make)
    env.made = made
    return env


def write_req(env, **kw) -> Path:
    p = env.tmp / "req.json"
    p.write_text(json.dumps(request(env, **kw).to_json()))
    return p


def test_cli_exit_codes(cli_env, capsys) -> None:
    env = cli_env
    assert cli.main(["generate", "--request", str(write_req(env)), "--base-url", "http://127.0.0.1:1"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "ok" and len(printed["frames"]) == 33
    assert env.made == {"base_url": "http://127.0.0.1:1", "output_root": env.work / "output", "exec_container": None}

    env.made["status"] = "timeout"
    assert cli.main(["generate", "--request", str(write_req(env, out_dir=env.tmp / "o2"))]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "timeout"

    assert cli.main(["generate", "--request", str(write_req(env, direction="west", out_dir=env.tmp / "o3"))]) == 2
    assert cli.main(["generate", "--request", str(write_req(env, track="C", out_dir=env.tmp / "o4"))]) == 2
    assert cli.main(["generate", "--request", str(env.tmp / "missing.json")]) == 2
    bad = env.tmp / "bad.json"
    bad.write_text("{not json")
    assert cli.main(["generate", "--request", str(bad)]) == 2
    bad.write_text('{"track": "E"}')
    assert cli.main(["generate", "--request", str(bad)]) == 2
    assert not (env.tmp / "o3").exists() and not (env.tmp / "o4").exists()


def test_make_client_exec_uses_in_container_api(tmp_path: Path) -> None:
    from sonoranlab.comfy.client import PodmanExecTransport

    c = track_e.make_client("http://127.0.0.1:8189", tmp_path, exec_container="sonoranlab-comfy")
    assert c.base_url == "http://127.0.0.1:8188"
    assert isinstance(c._transport, PodmanExecTransport) and c._transport.container == "sonoranlab-comfy"
    assert track_e.make_client("http://127.0.0.1:8189", tmp_path).base_url == "http://127.0.0.1:8189"


def test_term_becomes_keyboard_interrupt_and_handlers_restore() -> None:
    import os
    import signal

    from sonoranlab.cli import _cancel_on_term

    before = signal.getsignal(signal.SIGTERM)

    def body() -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    with pytest.raises(KeyboardInterrupt):
        _cancel_on_term(body)
    assert signal.getsignal(signal.SIGTERM) == before
