"""Typed job wrappers over `sonoranlab compose` and `sonoranlab deliver build` (DYL-130, ADR-0010/0011 patterns).

Inputs are job/registry/campaign ids and STATE-staged files only; each runs as a durable job (the stdio server never
spawns processes) whose output is a fresh directory under the job's STATE output dir. There is deliberately no apply,
verify, rollback or install tool: applying to a disposable snapshot is a coordinator/owner CLI action and production
installation is owner-only.
"""

from __future__ import annotations

from pathlib import Path

from sonoranlab import campaign
from sonoranlab import registry as records
from sonoranlab.api import paths
from sonoranlab.api.registry import Context, Tool, ToolError
from sonoranlab.artifacts import require_regular_file, sha256_file
from sonoranlab.jobs import manager, store

_ID = {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]*$", "maxLength": 128}
_JOB_ID = {"type": "string", "pattern": f"^{store.JOB_ID}$"}
_CLIP_JOB = {"type": "string", "pattern": f"^({store.JOB_ID}|import-[a-z0-9]+)$"}
_STAGED = {"type": "string", "minLength": 1, "maxLength": 1024, "description": "Path relative to STATE/staged."}
_KEY = {"type": "string", "minLength": 1}
LIMIT = 20


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _files(root: Path) -> dict:
    return {str(p): sha256_file(p) for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


def _native(job_id: str, kind: str | None, field: str) -> tuple[dict, Path]:
    job = records._native_job(job_id)
    if job is None:
        raise ToolError("not_found", f"Unknown job {job_id}", field=field)
    if kind and job["kind"] != kind:
        raise ToolError("invalid_params", f"{job_id} is a {job['kind']} job, not {kind}", field=field)
    if job["state"] != "succeeded":
        raise ToolError("not_ready", f"{job_id} is {job['state']}", field=field)
    return job, store.output(job)


def _clip_job(identifier: str, ctx: Context) -> Path:
    """The final validated job directory behind a native job id (animate: its sweep final job) or an import id."""
    if records._native_job(identifier):
        job, root = _native(identifier, None, "clips")
        if job["kind"] == "animate":
            doc = records._json(records._job_file(root, "animate/animate.json"), code="invalid_params")
            return records._animate_final(doc, root / "animate", ctx)
        return records._job_directory(root, ctx)
    return records._review_directory(records._find_job(identifier, ctx), ctx)


def _approved_stills(project: str, character: str, reference_id: str, ctx: Context) -> Path:
    base = records._base(project, character, ctx)
    if not (base / "character.json").exists():
        raise ToolError("not_found", "Character has no registered references", field="reference_id")
    sets = records._record(base, project, character)["reference_sets"]
    refs = next((r for r in sets if r["ref_set_id"] == reference_id), None)
    if refs is None:
        raise ToolError("not_found", "Unknown reference set for this character", field="reference_id")
    approval = records._verify_refs(base, refs, ctx)["approval"]
    if approval["state"] != "approved":
        raise ToolError(
            "refused", f"Reference set is {approval['state']}; owner approval required", field="reference_id"
        )
    return records._directory(base / "refs" / reference_id, ctx)


def _staged(relative: str, field: str, ctx: Context) -> Path:
    root = paths.state_root() / "staged"
    rel = paths._path(relative)
    if rel.is_absolute():
        raise ToolError("path_refused", "Staged files are named relative to STATE/staged", field=field)
    target = paths._confine(root / rel, (root,))
    records._protected(target, ctx)
    try:
        require_regular_file(root / rel, root=root)
    except ValueError as exc:
        raise ToolError("path_refused", str(exc), field=field) from None
    return target


class ComposeSheet:
    gpu = False
    heavy_cpu = False  # a re-validation of existing cells; no engine, no model

    def need_gib(self, params: dict) -> None:
        return None

    def _inputs(self, params: dict, ctx: Context) -> tuple:
        profile = records._character(params["project"], params["character"], ctx)
        names = [c["name"] for c in params["clips"]]
        if len(set(names)) != len(names):
            raise ToolError("invalid_params", "Each clip appears once", field="clips")
        if unknown := [n for n in names if n not in profile.character(params["character"]).animations]:
            raise ToolError("invalid_params", f"Unknown profile clips {unknown}", field="clips")
        if params.get("campaign_id"):
            campaign.check_composition(params["campaign_id"], params, ctx)  # DYL-129: selected candidates only
        jobs = [(c["name"], _clip_job(c["job"], ctx)) for c in params["clips"]]
        stills = _approved_stills(params["project"], params["character"], params["reference_id"], ctx)
        return profile, jobs, stills

    def validate(self, params: dict) -> tuple[dict, dict]:
        profile, jobs, stills = self._inputs(params, Context())
        hashes = {str(profile.path): sha256_file(profile.path)} | _files(stills)
        for _, root in jobs:
            for name in ("report.json", "manifest.json"):
                hashes[str(root / name)] = sha256_file(records._job_file(root, name))
        return {**params, "anim": "compose"}, hashes

    def run(self, job: dict, ctx) -> dict:
        from PIL import Image

        from sonoranlab.compose import ComposeError, compose

        params, hashes = self.validate(job["params"])
        if hashes != job["input_hashes"]:
            raise ToolError("refused", "Frozen job inputs changed before execution")
        api_ctx = Context()
        profile, jobs, stills = self._inputs(params, api_ctx)
        character = params["character"]
        ctx.set_stage("validate_export")
        out = paths._confine(ctx.output_dir, (paths.state_root(),)) / "composed"
        try:
            res = compose(profile=profile.path, character=character, clips=jobs, stills=stills, out=out)
        except ComposeError as exc:
            raise ToolError("refused", str(exc)) from None
        result = {
            "pass": res["pass"],
            "exit": res["exit"],
            "failures": res["failures"],
            "warnings": res["warnings"],
            "report": str(out / "report.json"),
            "technical": {"validator": "pass" if res["pass"] else "fail", "godot": "not_run"},
        }
        if res["pass"]:
            sheet = out / "export" / character / f"{character}.png"
            with Image.open(sheet) as image:
                size = list(image.size)
            # Registered so godot/smoke/view tools can name it; the owner's review binds the same directory.
            result["artifact_id"] = records.import_job(params["project"], character, str(out), ctx=api_ctx)[
                "artifact_id"
            ]
            result["export"] = str(sheet)
            if params.get("campaign_id"):
                campaign.record_composed(
                    params["campaign_id"], params, job["job_id"], result["artifact_id"], sha256_file(sheet), api_ctx
                )
            result["summary"] = {
                "sheet_sha256": sha256_file(sheet),
                "dimensions": size,
                "clips": [c["name"] for c in params["clips"]],
                "owner_review_dir": f"$SONORANLAB_STATE/{job['output_dir']}/composed",
            }
        return result

    def stage(self, job: dict) -> dict:
        return {"stage": job["stage"]}


class BuildDelivery:
    gpu = False
    heavy_cpu = False

    def need_gib(self, params: dict) -> None:
        return None

    def _inputs(self, params: dict, ctx: Context) -> tuple:
        from sonoranlab.review import review_errors

        profile = records._profile(params["project"], ctx)
        composed_job, root = _native(params["composed"], "compose_sheet", "composed")
        composed = records._job_directory(root / "composed", ctx)
        if problems := review_errors(composed):
            raise ToolError(
                "not_ready",
                "Composed sheet is not production usable: " + "; ".join(p["message"] for p in problems[:3]),
                field="composed",
                hint="the owner reviews the sheet at 1x and runs `sonoranlab review record <owner_review_dir>`",
            )
        try:
            frozen = campaign._load(campaign._find(params["campaign_id"]))[1][-1]
        except campaign.LedgerError as exc:
            raise ToolError("conflict", f"Campaign ledger is damaged: {exc}") from None
        if (frozen["project"], frozen["character"]) != (params["project"], composed_job["character"]):
            raise ToolError("refused", "Campaign and composed sheet name different characters", field="campaign_id")
        staged = {k: _staged(params[k], k, ctx) for k in ("base_manifest", "import_template")}
        return profile, composed, frozen, staged

    def validate(self, params: dict) -> tuple[dict, dict]:
        profile, composed, frozen, staged = self._inputs(params, Context())
        files = [profile.path, composed / "review.json", composed / "report.json", *staged.values()]
        hashes = {str(p): sha256_file(p) for p in files} | {"request": frozen["request_sha256"]}
        return {**params, "character": frozen["character"], "anim": "delivery"}, hashes

    def run(self, job: dict, ctx) -> dict:
        from sonoranlab import deliver

        params, hashes = self.validate(job["params"])
        if hashes != job["input_hashes"]:
            raise ToolError("refused", "Frozen job inputs changed before execution")
        profile, composed, frozen, staged = self._inputs(params, Context())
        work = paths._confine(ctx.output_dir, (paths.state_root(),))
        store.atomic(work / "request.json", frozen["request"], exclusive=True)  # the admitted, frozen request
        ctx.set_stage("check")
        try:
            res = deliver.build(
                profile=profile.path,
                composed=composed,
                request=work / "request.json",
                target_rev=params["target_rev"],
                base_manifest=staged["base_manifest"],
                import_template=staged["import_template"],
                out=work / "bundle",
            )
        except (deliver.DeliveryError, OSError) as exc:
            raise ToolError("refused", str(exc)) from None
        return {
            "ok": True,
            "ref": f"{job['job_id']}:bundle/bundle.json",
            "summary": {
                "state": res["state"],
                "files": res["files"],
                "known_missing_clips": [c["name"] for c in res["known_missing_clips"]],
                "playable_class": not res["known_missing_clips"],
                "next": "coordinator: `sonoranlab deliver apply|verify` in a disposable snapshot; owner installs",
            },
        }

    def stage(self, job: dict) -> dict:
        return {"stage": job["stage"]}


EXECUTORS = {"compose_sheet": ComposeSheet(), "build_delivery": BuildDelivery()}


def submit(kind: str, params: dict, ctx: Context) -> dict:
    # Admission errors (unapproved references, missing owner review, staged-path refusals) surface at submit time.
    return manager.submit(kind, params, params.get("idempotency_key"))


def delivery_status(bundle: str | None = None, character: str | None = None, *, ctx: Context) -> dict:
    from sonoranlab import deliver

    jobs = [
        j
        for j in manager.reconcile_all()
        if j["kind"] == "build_delivery" and j["state"] == "succeeded" and (bundle is None or j["job_id"] == bundle)
    ]
    if bundle and not jobs:
        raise ToolError("not_found", "Unknown or unfinished delivery bundle", field="bundle")
    bundles = []
    for job in jobs:
        if character and job["character"] != character:
            continue
        path = store.output(job) / "bundle"
        try:
            doc = deliver.load_bundle(records._job_directory(path, ctx))
        except (deliver.DeliveryError, ToolError, OSError, KeyError, TypeError) as exc:
            bundles.append({"bundle": job["job_id"], "state": "INVALID", "problem": str(exc)[:500]})
            continue
        bundles.append(
            {
                "bundle": job["job_id"],
                "character": doc["character"],
                "state": doc["state"],
                "states": {k: v["value"] for k, v in doc["states"].items()},
                "target_revision": doc["target"]["revision"],
                "known_missing_clips": [c["name"] for c in doc["known_missing_clips"]],
                "playable_class": doc["playable_class"],
            }
        )
    return {"bundles": bundles[-50:], "count": len(bundles), "installed_by": "the owner only (INSTALL.md)"}


TOOLS = (
    Tool(
        "compose_sheet",
        "Compose selected per-clip jobs + the approved stills into one re-validated class sheet (job).",
        _schema(
            {
                "project": _ID,
                "character": _ID,
                "clips": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 8,
                    "items": _schema({"name": _ID, "job": _CLIP_JOB}, ["name", "job"]),
                    "description": "Sheet order, e.g. idle then run.",
                },
                "reference_id": {"type": "string", "pattern": "^refs-[0-9a-f]{12}$"},
                "campaign_id": {"type": "string", "pattern": f"^{campaign.CAMPAIGN_ID}$"},
                "idempotency_key": _KEY,
            },
            ["project", "character", "clips", "reference_id"],
        ),
        False,
        "job",
        lambda p, c: submit("compose_sheet", p, c),
    ),
    Tool(
        "build_delivery",
        "Stage a delivery bundle in STATE from an owner-reviewed composed sheet (job); never touches the game.",
        _schema(
            {
                "project": _ID,
                "campaign_id": {"type": "string", "pattern": f"^{campaign.CAMPAIGN_ID}$"},
                "composed": _JOB_ID,
                "target_rev": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
                "base_manifest": _STAGED,
                "import_template": _STAGED,
                "idempotency_key": _KEY,
            },
            ["project", "campaign_id", "composed", "target_rev", "base_manifest", "import_template"],
        ),
        False,
        "job",
        lambda p, c: submit("build_delivery", p, c),
    ),
    Tool(
        "delivery_status",
        "Read delivery bundle states (BUILT / READY_TO_INSTALL) with integrity rechecked; never installs.",
        _schema({"bundle": _JOB_ID, "character": _ID}, []),
        True,
        "sync",
        lambda p, c: delivery_status(**p, ctx=c),
    ),
)
