"""Engine fakes at the existing executor hook (SONORANLAB_JOB_EXECUTORS): real admission/validation, no Godot."""

import json
import shutil

from sonoranlab.api.registry import Context
from sonoranlab.jobs.kinds.deterministic import EXECUTORS as DETERMINISTIC
from sonoranlab.jobs.kinds.smoke import EXECUTORS as SMOKE


class Wrapped:
    def __init__(self, original):
        self.original = original

    def __getattr__(self, name):
        return getattr(self.original, name)


class FakeGodot(Wrapped):
    def run(self, job, ctx):
        self.original.validate(job["params"])
        return {
            "technical": {"validator": None, "godot": "unavailable"},
            "godot_check": {"status": "unavailable", "available": False, "godot_attempts": []},
        }


class FakeSmoke(Wrapped):
    def run(self, job, ctx):
        _, candidate, siblings = self.original._admit(job["params"], Context())
        out = ctx.output_dir / "smoke"
        out.mkdir()
        sheet = next(candidate.rglob("*.png"))
        shutil.copyfile(sheet, out / "render_1x.png")
        (out / "smoke.json").write_text(json.dumps({"status": "pass", "siblings": sorted(siblings), "fake": True}))
        return {
            "smoke": {"status": "pass", "engine": "fake", "failed_checks": []},
            "renders": 1,
            "ref": f"{job['job_id']}:smoke/smoke.json",
            "agent_recommendation": "",
            "owner_style_acceptance": "",
            "technical": {"validator": None, "godot": "pass"},
        }


EXECUTORS = {"godot_check": FakeGodot(DETERMINISTIC["godot_check"]), "smoke_render": FakeSmoke(SMOKE["smoke_render"])}
