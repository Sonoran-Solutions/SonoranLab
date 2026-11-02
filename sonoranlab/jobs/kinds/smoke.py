"""`smoke_render` job (DYL-131): the isolated side-by-side Godot render, admitted from registry/STATE artifacts only."""

from __future__ import annotations

from pathlib import Path

from sonoranlab import registry as records
from sonoranlab import smoke
from sonoranlab.api import paths
from sonoranlab.api.registry import Context, ToolError
from sonoranlab.api.tools import deterministic as ops
from sonoranlab.artifacts import sha256_file
from sonoranlab.jobs import store

SHADER_RELATIVE = "smoke/PixelStandee.gdshader"  # the owner's private staged copy, under STATE; absent = no border
TIMEOUT_S = 900.0


def _export_dir(ref: str, label: str | None, ctx: Context) -> tuple[Path, str]:
    """Registry/job/artifact refs only (no paths): the export directory and the single character it holds."""
    root = ops._reference(ref, ctx)[0]
    root = ops.resolve_input_dir(root if root.is_dir() else root.parent, ctx)
    names = {p.name.removesuffix(".sonoranlab.json") for p in smoke._sidecars(root, label)}
    if len(names) != 1:
        raise ToolError(
            "refused", f"{ref}: expected exactly one export sidecar, found {sorted(names)}", field="candidate"
        )
    return root, names.pop()


def _shader() -> Path | None:
    path = paths.state_root() / SHADER_RELATIVE
    if path.is_symlink() or not path.exists():
        if path.is_symlink():
            raise ToolError("path_refused", "Staged shader must not be a symlink")
        return None
    return paths.resolve_input(path)


class SmokeRender:
    gpu = False
    heavy_cpu = True  # one heavy Godot job per host: shares the scheduler lock with the other heavy kinds

    def need_gib(self, params: dict) -> None:
        return None

    def _admit(self, params: dict, ctx: Context):
        profile = records._character(params["project"], params["character"], ctx)
        if len(params["siblings"]) > smoke.MAX_SIBLINGS or len(set(params["siblings"])) != len(params["siblings"]):
            raise ToolError(
                "invalid_params", f"Expected up to {smoke.MAX_SIBLINGS} distinct sibling refs", field="siblings"
            )
        candidate, label = _export_dir(params["candidate"], params["character"], ctx)
        siblings = {}
        for ref in params["siblings"]:
            root, name = _export_dir(ref, None, ctx)
            if name == label or name in siblings:
                raise ToolError("invalid_params", f"Sibling {name!r} repeats a character", field="siblings")
            siblings[name] = root
        return profile, candidate, siblings

    def validate(self, params: dict) -> tuple[dict, dict]:
        ctx = Context()
        profile, candidate, siblings = self._admit(params, ctx)
        hashes = {str(profile.path): sha256_file(profile.path)}
        for root in (candidate, *siblings.values()):
            for side in smoke._sidecars(root):
                sheet = side.parent / (side.name.removesuffix(".sonoranlab.json") + ".png")
                for file in (side, sheet):
                    hashes[str(file)] = sha256_file(ops.input_file(str(file), ctx))
        if shader := _shader():
            hashes[str(shader)] = sha256_file(shader)
        # The job store files outputs under project/character/anim; a render has no clip, so it uses a fixed slot.
        return {**params, "anim": "smoke"}, hashes

    def run(self, job: dict, ctx) -> dict:
        params, hashes = self.validate(job["params"])
        if hashes != job["input_hashes"]:
            raise ToolError("refused", "Frozen job inputs changed before execution")
        api_ctx = Context()
        profile, candidate, siblings = self._admit(params, api_ctx)
        out = paths._confine(ctx.output_dir, (paths.state_root(),))
        if any(out.iterdir()):
            raise ToolError("conflict", "Job output must be empty before execution")
        ctx.set_stage("check")
        try:
            doc = smoke.run_smoke(
                profile=profile.path,
                candidate=candidate,
                siblings=siblings,
                out=out / "smoke",
                shader=_shader(),
                timeout=TIMEOUT_S,
                cancelled=lambda: ctx.cancelled,
            )
        except smoke.SmokeError as exc:
            raise ToolError("refused", str(exc)) from exc
        ctx.check_cancelled()
        verdict = {"pass": "pass", "fail": "fail", "unavailable": "unavailable"}[doc["status"]]
        ref = ops.artifact(out, out / "smoke/smoke.json", job["job_id"])
        failed = [k for k, v in doc["checks"].items() if not v["pass"]]
        return {
            "smoke": {k: doc[k] for k in ("status", "engine", "border", "godot")} | {"failed_checks": failed},
            "renders": len(doc["renders"]),
            "ref": ref,
            "agent_recommendation": "",
            "owner_style_acceptance": "",
            "technical": {"validator": None, "godot": verdict},
        }

    def stage(self, job: dict) -> dict:
        root = store.output(job)
        return {"stage": job["stage"], "smoke_json": (root / "smoke/smoke.json").is_file()}


EXECUTORS = {"smoke_render": SmokeRender()}
