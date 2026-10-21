"""Track C adapter with a fake ComfyUI client: CPU only, no server, no ~/sonoranlab-work."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from PIL import Image

from sonoranlab import cli
from sonoranlab.comfy.adapters import track_c
from sonoranlab.comfy.adapters.base import DEFAULT_BACKGROUND, GenerateRequest, RawResult
from sonoranlab.comfy.adapters.track_c import POSE_PROMPTS, QWEN_NEED_GIB, TrackC
from sonoranlab.comfy.adapters.track_e import guarded_argv
from sonoranlab.prep import start_image_from_rgba

DIRS = ["north", "north-east", "east", "south-east", "south"]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def write_still(path: Path, cell: int = 16) -> Path:
    im = Image.new("RGBA", (cell, cell), (0, 0, 0, 0))
    im.paste((180, 40, 40, 255), (4, 4, cell - 4, cell - 4))
    im.save(path)
    return path


def write_pose(path: Path, edge: int = 32) -> Path:
    im = Image.new("RGB", (edge, edge), (0, 0, 0))
    im.paste((0, 255, 0), (edge // 4, edge // 4, edge // 2, edge // 2))
    im.save(path)
    return path


class FakeClient:
    """Writes one RGB (node 9) and one RGBA (node 92) image under output_root, returns a RunRecord-shaped object."""

    def __init__(
        self,
        output_root: Path,
        *,
        rgb: int = 1,
        rgba: int = 1,
        status: str = "success",
        boom: Exception | None = None,
    ) -> None:
        self.root, self.rgb, self.rgba, self.status, self.boom = output_root, rgb, rgba, status, boom
        self.calls: list[dict[str, Any]] = []

    def run(self, workflow, values, *, timeout_s, expect, prompt_id=None):
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
        outs = []
        for node, key, count in (("9", "output_prefix", self.rgb), ("92", "output_prefix_rgba", self.rgba)):
            prefix = values[key]
            (self.root / prefix).parent.mkdir(parents=True, exist_ok=True)
            for i in range(count):
                a = np.zeros((8, 8, 4), np.uint8)
                a[2:6, 2:6] = (200, 30, 30, 255)
                if node == "9":
                    a[..., 3] = 255  # SaveImage RGB: fully opaque
                p = self.root / f"{prefix}_{i + 1:05d}_.png"
                Image.fromarray(a, "RGBA").convert("RGB" if node == "9" else "RGBA").save(p)
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
            prompt_id=prompt_id or "pid-c",
            workflow_source="qwen_edit_pose_native.json",
            workflow_source_sha256="a" * 64,
            workflow_patched_sha256="b" * 64,
            params=dict(values),
            models={"qwen.safetensors": "c" * 64},
            outputs=outs,
            status=self.status,
            timing={"exec_s": 3.5, "wall_s": 4.0},
            error=err,
        )
        rec.to_json = lambda: {"prompt_id": rec.prompt_id, "status": self.status}
        return rec


@pytest.fixture
def env(tmp_path: Path):
    work = tmp_path / "work"
    return SimpleNamespace(
        tmp=tmp_path,
        work=work,
        still=write_still(tmp_path / "still.png"),
        pose=write_pose(tmp_path / "pose.png"),
        client=FakeClient(work / "output"),
    )


def request(env, **kw) -> GenerateRequest:
    base = dict(
        track="C",
        character="samurai",
        anim="run",
        direction="east",
        source_image=env.still,
        prompt="",
        seed=7,
        out_dir=env.tmp / "attempts" / "c1",
        control_image=env.pose,
    )
    return GenerateRequest(**{**base, **kw})


def adapter(env) -> TrackC:
    return TrackC(env.client, work_root=env.work, workflow=object(), timeout_s=99)


def test_success_layout_and_values(env) -> None:
    req = request(env)
    res = adapter(env).generate(req)
    out = req.out_dir
    assert res.status == "ok" and res.error is None
    assert len(res.raw_frames) == len(res.frames) == 1
    assert {p.name for p in out.iterdir()} == {
        "submission.json",
        "source",
        "image_rgb",
        "image_rgba",
        "request.json",
        "run.json",
        "result.json",
    }
    assert {p.name for p in (out / "source").iterdir()} == {
        "source_image.png",
        "control_image.png",
        "reference_1024.png",
        "pose_1024.png",
    }
    assert res.frames[0].parent.name == "image_rgba" and res.raw_frames[0].parent.name == "image_rgb"
    assert res.outputs[f"image_rgba/{res.frames[0].name}"] == sha(res.frames[0])
    assert res.outputs[f"image_rgb/{res.raw_frames[0].name}"] == sha(res.raw_frames[0])
    assert len(res.outputs) == 2
    assert RawResult.from_json(json.loads((out / "result.json").read_text())) == res
    assert json.loads((out / "request.json").read_text()) == req.to_json()
    assert (
        res.prompt_id == json.loads((req.out_dir / "submission.json").read_text())["prompt_id"]
        and res.models == {"qwen.safetensors": "c" * 64}
        and res.timing["wall_s"] == 4.0
    )

    (call,) = env.client.calls
    v = call["values"]
    attempt = Path(v["reference_image"]).parent.name
    assert v["reference_image"] == f"sonoranlab/{attempt}/reference.png"
    assert v["control_image"] == f"sonoranlab/{attempt}/pose.png"
    assert attempt.startswith("c1-")
    staged_ref = env.work / "input" / attempt / "reference.png"
    staged_pose = env.work / "input" / attempt / "pose.png"
    assert sha(staged_ref) == sha(out / "source/reference_1024.png") == res.inputs["reference_image"]
    assert sha(staged_pose) == sha(out / "source/pose_1024.png") == res.inputs["control_image"]
    assert v["output_prefix"] == f"p3/{attempt}/raw" and v["output_prefix_rgba"] == f"p3/{attempt}/rgba"
    # cell 16 -> gen canvas 48; the reference inputs are 1024 (Qwen's ~1 MP encoder resolution)
    assert (v["width"], v["height"], v["steps"], v["cfg"], v["lightning_strength"], v["seed"]) == (48, 48, 4, 1, 1.0, 7)
    assert "negative_prompt" in v
    assert call["expect"] == {"9": 1, "92": 1} and call["timeout_s"] == 99

    with Image.open(staged_ref) as im:
        assert im.size == (1024, 1024) and im.getpixel((0, 0)) == (128, 128, 128)  # grey default
    with Image.open(staged_pose) as im:
        assert im.size == (1024, 1024) and im.getpixel((0, 0)) == (0, 0, 0)  # pose canvas is black
    src = res.checks["source"]
    assert src["kinds"] == ["still_rgba", "pose_render"]
    assert (src["cell"], src["canvas"], src["reference_canvas"]) == (16, 48, 1024)
    assert src["pose_source_scale"] == 2  # 32 / 16
    assert src["source_sha256"] == sha(env.still) and src["control_sha256"] == sha(env.pose)
    assert src["reference_sha256"] == res.inputs["reference_image"]
    assert src["pose_sha256"] == res.inputs["control_image"]
    assert res.checks["alpha"]["background"] == DEFAULT_BACKGROUND and res.checks["alpha"]["empty_frames"] == []


def test_pose_and_reference_map_to_their_own_load_image_params(env) -> None:
    """The skeleton is image1 (node 42, `control_image`) and the character still image2 (node 41, `reference_image`)."""
    adapter(env).generate(request(env))
    v = env.client.calls[0]["values"]
    assert v["control_image"].endswith("/pose.png") and v["reference_image"].endswith("/reference.png")
    # each staged file is the one its own prep produced, so a swap would fail the pixel checks
    with Image.open(env.work / "input" / Path(v["reference_image"]).parent.name / "reference.png") as ref:
        assert ref.getpixel((0, 0)) == (128, 128, 128)
    with Image.open(env.work / "input" / Path(v["control_image"]).parent.name / "pose.png") as pose:
        assert pose.getpixel((0, 0)) == (0, 0, 0)


def test_canvas_for_samurai_cell(env) -> None:
    req = request(
        env, source_image=write_still(env.tmp / "samurai.png", 184), control_image=write_pose(env.tmp / "p4.png", 736)
    )
    adapter(env).generate(req)
    v = env.client.calls[0]["values"]
    assert (v["width"], v["height"]) == (560, 560)


@pytest.mark.parametrize("direction", DIRS)
def test_default_prompt_per_direction(env, direction: str) -> None:
    adapter(env).generate(request(env, direction=direction))
    prompt = env.client.calls[0]["values"]["prompt"]
    assert prompt == POSE_PROMPTS[direction]
    assert "Picture 1" in prompt and "Picture 2" in prompt and "grey background" in prompt
    assert "magenta" not in prompt and "ControlNet" not in prompt
    assert len(set(POSE_PROMPTS.values())) == 5


def test_prompt_override_and_settings_override(env) -> None:
    res = adapter(env).generate(
        request(
            env,
            prompt="  custom  ",
            settings={"steps": 6, "negative_prompt": "blur", "lightning_strength": 0.8, "cfg": 2},
        )
    )
    v = env.client.calls[0]["values"]
    assert v["prompt"] == "custom" and "neg" not in v
    assert (v["steps"], v["negative_prompt"], v["lightning_strength"], v["cfg"]) == (6, "blur", 0.8, 2)
    assert res.status == "ok" and res.request.settings["steps"] == 6


def test_custom_background_needs_an_explicit_prompt(env) -> None:
    res = adapter(env).generate(request(env, background="#ff00ff", prompt="keep magenta"))
    assert res.status == "ok"
    with Image.open(res.request.out_dir / "source/reference_1024.png") as im:
        assert im.getpixel((0, 0)) == (255, 0, 255)


def test_client_failure_keeps_partial_outputs(env) -> None:
    env.client = FakeClient(env.work / "output", rgb=1, rgba=0, status="timeout")
    req = request(env)
    res = adapter(env).generate(req)
    assert res.status == "failed" and res.error == {
        "code": "timeout",
        "stage": "wait",
        "message": "timed out",
        "retryable": True,
    }
    assert len(res.raw_frames) == 1 and res.frames == []
    assert res.raw_frames[0].exists() and len(list((req.out_dir / "image_rgb").iterdir())) == 1
    assert (req.out_dir / "run.json").exists()
    assert RawResult.from_json(json.loads((req.out_dir / "result.json").read_text())).status == "failed"


def test_success_status_but_missing_image_is_failed(env) -> None:
    env.client = FakeClient(env.work / "output", rgba=0)
    res = adapter(env).generate(request(env))
    assert res.status == "failed" and res.error["code"] == "missing_frames" and res.error["retryable"] is True


def test_client_exception_is_a_failed_result(env) -> None:
    env.client = FakeClient(env.work / "output", boom=RuntimeError("socket gone"))
    req = request(env)
    res = adapter(env).generate(req)
    assert res.status == "failed" and res.error["code"] == "adapter_exception" and "socket gone" in res.error["message"]
    assert (req.out_dir / "request.json").exists() and (req.out_dir / "result.json").exists()


def test_staging_error_is_a_failed_result(env, monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(track_c.shutil, "copyfile", boom)
    res = adapter(env).generate(request(env))
    assert res.status == "failed" and res.error["stage"] == "stage" and env.client.calls == []


@pytest.mark.parametrize(
    "kw",
    [
        {"track": "E"},
        {"direction": "west"},
        {"direction": "up"},
        {"source_image": Path("nope.png")},
        {"control_image": None},
        {"control_image": Path("nope.png")},
        {"settings": {"bogus": 1}},
        {"settings": {"seed": 1}},
        {"settings": {"prompt": "x"}},
        {"settings": {"control_image": "x.png"}},
        {"settings": {"reference_image": "x.png"}},
        {"settings": {"output_prefix": "x"}},
        {"settings": {"output_prefix_rgba": "x"}},
        {"background": "grey"},
        {"seed": -1},
        {"anim": ""},
        {"workflow": "/etc/passwd"},
    ],
)
def test_malformed_request_writes_nothing(env, kw) -> None:
    req = request(env, **kw)
    with pytest.raises(ValueError):
        adapter(env).generate(req)
    assert not req.out_dir.exists() and not env.work.exists() and env.client.calls == []


def test_malformed_inputs_written_nothing(env) -> None:
    rgb_still = env.tmp / "rgb.png"
    Image.new("RGB", (16, 16), (0, 0, 0)).save(rgb_still)  # no alpha: not a cell-size RGBA still
    wide_still = env.tmp / "wide.png"
    Image.new("RGBA", (16, 24), (0, 0, 0, 0)).save(wide_still)
    bad_pose = write_pose(env.tmp / "p5.png", 20)  # not a multiple of cell 16
    for kw in ({"source_image": rgb_still}, {"source_image": wide_still}, {"control_image": bad_pose}):
        req = request(env, **kw)
        with pytest.raises(ValueError):
            adapter(env).generate(req)
        assert not req.out_dir.exists() and not env.work.exists() and env.client.calls == []


def test_default_prompt_refused_on_a_non_grey_background(env) -> None:
    req = request(env, background="#ff00ff")
    with pytest.raises(ValueError, match="grey"):
        adapter(env).generate(req)
    assert not req.out_dir.exists() and not env.work.exists()


def test_existing_out_dir_and_staging_refused(env) -> None:
    req = request(env)
    req.out_dir.mkdir(parents=True)
    with pytest.raises(ValueError, match="exists"):
        adapter(env).generate(req)
    assert list(req.out_dir.iterdir()) == []
    req2 = request(env, out_dir=env.tmp / "b")
    (env.work / "input").mkdir(parents=True)
    assert adapter(env).generate(req2).status == "ok"  # first use of the staging dir succeeds ...
    req3 = request(env, out_dir=env.tmp / "b2")
    (env.work / "input" / f"b2-{hashlib.sha256(str((env.tmp / 'b2').resolve()).encode()).hexdigest()[:10]}").mkdir()
    with pytest.raises(ValueError, match="staging"):
        adapter(env).generate(req3)  # ... a leftover one is never reused


def test_qwen_budget() -> None:
    assert QWEN_NEED_GIB == 60
    assert guarded_argv(["sonoranlab", "generate"], QWEN_NEED_GIB)[2:5] == ["--need-gib", "60", "--"]


def test_result_feeds_track_e_as_start_image(env) -> None:
    """A Track C RGBA output composited on grey is an RGB PNG, which Track E accepts as a posed start canvas."""
    res = adapter(env).generate(request(env))
    start = env.tmp / "start.png"
    info = start_image_from_rgba(res.frames[0], start)
    assert info["size"] == [8, 8] and hashlib.sha256(start.read_bytes()).hexdigest() == info["sha256"]
    with Image.open(res.raw_frames[0]) as im:
        assert im.mode == "RGB"  # node 9 is the raw RGB output
    with Image.open(start) as im:
        assert im.mode == "RGB" and im.getpixel((0, 0)) == (128, 128, 128) and im.getpixel((3, 3)) == (200, 30, 30)


# -- CLI ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def cli_env(env, monkeypatch, lease_window):
    lease_window(env.work, need_gib=60)
    monkeypatch.setattr(cli, "_exec", lambda *a: pytest.fail("CLI test must remain inside the fake guard"))
    monkeypatch.setenv("SONORANLAB_WORK", str(env.work))
    monkeypatch.setattr(track_c, "load_workflow", lambda p: object())
    made = {}

    def make(base_url, output_root, exec_container=None):
        made.update(base_url=base_url, output_root=output_root)
        return FakeClient(output_root, status=made.get("status", "success"))

    monkeypatch.setattr(track_c, "make_client", make)
    env.made = made
    return env


def write_req(env, **kw) -> Path:
    p = env.tmp / "req.json"
    p.write_text(json.dumps(request(env, **kw).to_json()))
    return p


def test_cli_dispatches_track_c(cli_env, capsys) -> None:
    env = cli_env
    assert cli.main(["generate", "--request", str(write_req(env)), "--base-url", "http://127.0.0.1:1"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "ok" and printed["request"]["track"] == "C" and len(printed["frames"]) == 1
    assert env.made == {"base_url": "http://127.0.0.1:1", "output_root": env.work / "output"}

    env.made["status"] = "timeout"
    assert cli.main(["generate", "--request", str(write_req(env, out_dir=env.tmp / "o2"))]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "timeout"

    assert cli.main(["generate", "--request", str(write_req(env, direction="west", out_dir=env.tmp / "o3"))]) == 2
    assert cli.main(["generate", "--request", str(write_req(env, out_dir=env.tmp / "o4", track="D"))]) == 2
    assert not (env.tmp / "o3").exists() and not (env.tmp / "o4").exists()


def test_submission_is_persisted_before_the_prompt_is_posted(env) -> None:
    seen = {}
    req = request(env)

    class Recording(FakeClient):
        def run(self, workflow, values, *, timeout_s, expect, prompt_id=None):
            sub = json.loads((req.out_dir / "submission.json").read_text())
            seen.update(sub=sub, prompt_id=prompt_id)
            return super().run(workflow, values, timeout_s=timeout_s, expect=expect, prompt_id=prompt_id)

    res = TrackC(Recording(env.work / "output"), work_root=env.work, workflow=object()).generate(req)
    assert seen["sub"]["prompt_id"] == seen["prompt_id"] == res.prompt_id
    assert seen["sub"]["state"] == "submitting"
