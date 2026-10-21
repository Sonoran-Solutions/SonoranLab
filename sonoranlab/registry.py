"""Small, locked registry records; approval and human review are always verified on read."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager, suppress
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from sonoranlab.api import paths
from sonoranlab.api.registry import Context, ToolError
from sonoranlab.artifacts import require_regular_file, sha256_file

_ID = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_JOB = re.compile(r"job-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\Z")
_REF = re.compile(r"refs-[0-9a-f]{12}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ToolError("invalid_params", "Expected a plain registry id")
    return value


def _profile(project: str, ctx: Context):
    _id(project)
    profile = next((p for p in ctx.profiles() if p.id == project), None)
    if profile is None:
        raise ToolError("not_found", "Unknown project", field="project")
    return profile


def _character(project: str, character: str, ctx: Context):
    profile = _profile(project, ctx)
    _id(character)
    if character not in profile.characters:
        raise ToolError(
            "invalid_params",
            "Unknown profile character",
            field="character",
            hint="Add a characters: row to the project profile via a reviewed PR first.",
        )
    return profile


def _protected(path: Path, ctx: Context) -> None:
    for profile in ctx.profiles():
        if path.resolve().is_relative_to(profile.export.godot_project.resolve()):
            raise ToolError("path_refused", "Game checkout is protected")


def _directory(value: str | Path, ctx: Context) -> Path:
    path = paths._confine(paths._path(value), (*paths.read_roots(), paths.state_root()))
    _protected(path, ctx)
    if not path.is_dir():
        raise ToolError("path_refused", f"{path}: expected a directory")
    return path


def _job_directory(value: str | Path, ctx: Context) -> Path:
    job = _directory(value, ctx)
    if any(p.export.godot_project.resolve().is_relative_to(job) for p in ctx.profiles()):
        raise ToolError("path_refused", "Job directory contains a protected game checkout")
    return job


def _state(path: Path, ctx: Context, *, create: bool = False) -> Path:
    """Create parents one at a time through the shared exclusive-output resolver."""
    root = paths.state_root()
    _protected(path, ctx)
    if not path.is_relative_to(root):
        raise ToolError("path_refused", "Registry path is outside STATE")
    if create:
        # STATE itself is administrator configuration, not a caller-supplied output.
        root.mkdir(parents=True, exist_ok=True)
    for part in (*reversed(path.parents), path):
        if not part.is_relative_to(root) or part == root:
            continue
        if part.is_symlink():
            raise ToolError("path_refused", f"{part}: symlink in registry state")
        if part.exists():
            paths._confine(part, (root,))
            if not part.is_dir():
                raise ToolError("path_refused", f"{part}: expected a registry directory")
        elif create:
            try:
                paths.resolve_output(part).mkdir()
            except FileExistsError:
                if part.is_symlink() or not part.is_dir():
                    raise ToolError("path_refused", f"{part}: invalid registry directory") from None
    return path


def _base(project: str, character: str, ctx: Context, *, create: bool = False) -> Path:
    return _state(paths.state_root() / "registry" / _id(project) / _id(character), ctx, create=create)


def _json(path: Path, *, code: str = "internal") -> dict:
    from sonoranlab.review import _load

    try:
        paths.resolve_input(path)
        value = _load(path)
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        return value
    except FileNotFoundError:
        raise
    except (ValueError, OSError) as exc:
        raise ToolError(code, f"{path}: corrupt or unreadable JSON ({exc})") from exc


def _record(base: Path, project: str, character: str) -> dict:
    path = base / "character.json"
    if not path.exists() and not path.is_symlink():
        if any((base / name).exists() for name in ("refs", "approvals")):
            raise ToolError("not_found", f"{path}: missing registry record; refusing to reset existing evidence")
        return {
            "schema": "sonoranlab.character/1",
            "project": project,
            "character": character,
            "reference_sets": [],
            "jobs": [],
        }
    record = _json(path)
    string = {"type": "string"}
    file = {
        "type": "object",
        "required": ["sha256", "source_path"],
        "properties": {"sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}, "source_path": string},
    }
    schema = {
        "type": "object",
        "required": ["schema", "project", "character", "reference_sets", "jobs"],
        "properties": {
            "schema": {"const": "sonoranlab.character/1"},
            "project": {"const": project},
            "character": {"const": character},
            "reference_sets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["ref_set_id", "files", "registered_at"],
                    "properties": {
                        "ref_set_id": {"type": "string", "pattern": "^refs-[0-9a-f]{12}$"},
                        "registered_at": string,
                        "files": {
                            "type": "object",
                            "minProperties": 8,
                            "maxProperties": 8,
                            "propertyNames": {"pattern": "^[a-z0-9][a-z0-9_-]*$"},
                            "additionalProperties": file,
                        },
                    },
                },
            },
            "jobs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["job_id", "artifact_id", "job_dir", "review_job", "hashes", "provenance"],
                    "properties": {
                        "job_id": {"type": "string", "pattern": "^job-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$"},
                        "artifact_id": {"type": "string", "pattern": "^import-[0-9a-f]{24}$"},
                        "job_dir": string,
                        "review_job": string,
                        "provenance": {"type": "object"},
                        "hashes": {"type": "object", "additionalProperties": file["properties"]["sha256"]},
                    },
                },
            },
        },
    }
    try:
        Draft202012Validator(schema).validate(record)
    except ValidationError as exc:
        raise ToolError("internal", f"{path}: corrupt registry record ({exc.message})") from exc
    return record


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic(path: Path, record: dict) -> None:
    data = json.dumps(record, sort_keys=True, allow_nan=False) + "\n"
    if len(data.encode()) > 16 * 1024 * 1024:
        raise ToolError("conflict", f"{path}: registry JSON exceeds 16 MiB")
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".character-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _sync_dir(path.parent)
    finally:
        Path(temp).unlink(missing_ok=True)


@contextmanager
def _lock(base: Path):
    lock = base / "character.lock"
    if lock.exists() or lock.is_symlink():
        try:
            require_regular_file(lock, root=base)
        except ValueError as exc:
            raise ToolError("path_refused", str(exc)) from exc
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ToolError("path_refused", "Registry lock must be a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _ref_id(files: dict) -> str:
    pairs = sorted((direction, file["sha256"]) for direction, file in files.items())
    return "refs-" + hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode()).hexdigest()[:12]


def _verify_refs(base: Path, refs: dict, ctx: Context) -> dict:
    current, problems, evidence = {}, [], []
    root = _state(base / "refs" / refs["ref_set_id"], ctx)
    for direction, file in refs["files"].items():
        path = root / f"still_{direction}.png"
        try:
            require_regular_file(path, root=base)
            current[direction] = sha256_file(paths.resolve_input(path))
            if current[direction] != file["sha256"]:
                problems.append(f"{path.name}: sha256 changed")
        except (OSError, ValueError) as exc:
            current[direction] = None
            problems.append(f"{path.name}: {exc}")
    approvals = _state(base / "approvals", ctx)
    for path in sorted(approvals.glob("*.json")):
        approval = _json(path)
        if approval.get("ref_set_id") != refs["ref_set_id"]:
            continue
        evidence.append(
            {"path": path.relative_to(paths.state_root()).as_posix(), "sha256": sha256_file(path), "record": approval}
        )
        if (
            approval.get("schema") != "sonoranlab.ref-approval/1"
            or approval.get("project") != base.parent.name
            or approval.get("character") != base.name
            or approval.get("hashes") != current
            or not all(
                isinstance(approval.get(k), str) and approval[k].strip()
                for k in ("reviewer", "evidence", "approved_at")
            )
        ):
            problems.append(f"{path.name}: invalid approval binding or metadata")
    return {
        **refs,
        "verification": {"hashes": current, "problems": problems},
        "approval": {
            "state": "invalid" if problems else "approved" if evidence else "unapproved",
            "evidence": evidence,
            "problems": problems,
        },
    }


def _check_stills(profile, character: str, directory: Path, ctx: Context) -> None:
    # Reuse animate's full still-set admission without launching a generation or reading the palette.
    # ponytail: this also requires processing/non-rig square cells. Extract animate's
    # still checker when a project needing other cells is introduced.
    from sonoranlab.animate import UsageError, _preflight

    try:
        _preflight(
            profile.path,
            character,
            next(iter(profile.character(character).animations)),
            "E",
            [0],
            "default",
            40,
            False,
            directory,
            paths.state_root() / f".preflight-{uuid4().hex}",
            ctx.repo,
        )
    except UsageError as exc:
        raise ToolError("invalid_params", str(exc), field="reference_dir") from exc


def register_character(project: str, character: str, reference_dir: str, notes: str = "", *, ctx: Context) -> dict:
    profile = _character(project, character, ctx)
    if len(profile.directions.order) != 8:
        raise ToolError("invalid_params", "Reference registration requires eight profile directions")
    source = _directory(reference_dir, ctx)
    sources = {d: paths.resolve_input(source / f"still_{d}.png") for d in profile.directions.order}
    for path in sources.values():
        _protected(path, ctx)
    _check_stills(profile, character, source, ctx)
    files = {d: {"sha256": sha256_file(p), "source_path": str(p)} for d, p in sources.items()}
    ref_id = _ref_id(files)
    base = _base(project, character, ctx, create=True)
    with _lock(base):
        record = _record(base, project, character)
        existing = next((r for r in record["reference_sets"] if r["ref_set_id"] == ref_id), None)
        if existing is not None:
            if {d: f["sha256"] for d, f in existing["files"].items()} != {d: f["sha256"] for d, f in files.items()}:
                raise ToolError("conflict", f"Reference content-id collision: {ref_id}")
            verified = _verify_refs(base, existing, ctx)
            if verified["verification"]["problems"]:
                raise ToolError("conflict", f"Stored reference set {ref_id} was modified; refusing overwrite")
            return {"ref_set_id": ref_id, "approval": verified["approval"], "created": False}
        parent = _state(base / "refs", ctx, create=True)
        destination = paths.resolve_output(parent / ref_id)
        destination.mkdir()
        for direction, path in sources.items():
            target = destination / f"still_{direction}.png"
            with path.open("rb") as src, target.open("xb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            if sha256_file(target) != files[direction]["sha256"]:
                raise ToolError("conflict", f"{path}: source changed during registration")
            target.chmod(0o444)
        _check_stills(profile, character, destination, ctx)
        destination.chmod(0o555)
        _sync_dir(destination)
        _sync_dir(parent)
        refs = {"ref_set_id": ref_id, "files": files, "registered_at": datetime.now(UTC).isoformat(), "notes": notes}
        record["spec"] = _spec(profile, character)
        record["reference_sets"].append(refs)
        _atomic(base / "character.json", record)
    return {"ref_set_id": ref_id, "approval": {"state": "unapproved", "evidence": [], "problems": []}, "created": True}


def approve_references(
    project: str, character: str, ref_set: str, reviewer: str, evidence: str, *, ctx: Context
) -> dict:
    """Owner CLI only. Agents must never invoke this approval operation."""
    _character(project, character, ctx)
    if not _REF.fullmatch(ref_set) or not reviewer.strip() or not evidence.strip():
        raise ToolError("invalid_params", "A valid ref-set, reviewer and evidence are required")
    base = _base(project, character, ctx)
    if not base.exists():
        raise ToolError("not_found", "Character has no registered references")
    with _lock(base):
        record = _record(base, project, character)
        refs = next((r for r in record["reference_sets"] if r["ref_set_id"] == ref_set), None)
        if refs is None:
            raise ToolError("not_found", "Unknown reference set for this project/character")
        verified = _verify_refs(base, refs, ctx)
        if verified["verification"]["problems"]:
            raise ToolError("conflict", "Reference hashes no longer match registration")
        now = datetime.now(UTC)
        approval = {
            "schema": "sonoranlab.ref-approval/1",
            "project": project,
            "character": character,
            "ref_set_id": ref_set,
            "hashes": verified["verification"]["hashes"],
            "reviewer": reviewer,
            "evidence": evidence,
            "approved_at": now.isoformat(),
        }
        parent = _state(base / "approvals", ctx, create=True)
        path = paths.resolve_output(parent / (now.strftime("%Y%m%dT%H%M%S%fZ") + ".json"))
        with path.open("x") as stream:
            json.dump(approval, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o444)
        _sync_dir(parent)
    return approval


def _job_file(job: Path, relative: str) -> Path:
    rel = paths._path(relative)
    if rel.is_absolute():
        raise ToolError("path_refused", "Artifact path must be relative to its job")
    path = paths.resolve_input(job / rel)
    if not path.is_relative_to(job):
        raise ToolError("path_refused", "Artifact escapes its job")
    try:
        require_regular_file(job / rel, root=job)
    except ValueError as exc:
        raise ToolError("path_refused", str(exc)) from exc
    return path


def _job_metadata(job: Path, ctx: Context) -> tuple[Path, dict, dict | None]:
    """Find animate's contained final job, then admit metadata before review reads it."""
    animate = None
    if (job / "animate.json").exists() or (job / "animate.json").is_symlink():
        animate = _json(_job_file(job, "animate.json"), code="invalid_params")
        if (
            animate.get("schema") != "sonoranlab.animate/1"
            or not isinstance(animate.get("inputs"), dict)
            or animate.get("outcome") not in {"pass", "validator_fail", "blocked", "no_candidates", "error"}
            or type(animate.get("exit")) is not int
        ):
            raise ToolError("invalid_params", f"{job / 'animate.json'}: invalid animate metadata")
        verdict = animate.get("verdict")
        if isinstance(verdict, dict) and verdict.get("job"):
            final = _job_directory(verdict["job"], ctx)
            if not final.is_relative_to(job):
                raise ToolError("path_refused", "Animate final job escapes its directory")
            job = final
        else:
            if animate["outcome"] in {"pass", "validator_fail"}:
                raise ToolError("invalid_params", f"{job / 'animate.json'}: completed job lacks final verdict")
            return job, {}, animate
    report = _json(_job_file(job, "report.json"), code="invalid_params")
    manifest = _json(_job_file(job, "manifest.json"), code="invalid_params")
    if (
        type(report.get("pass")) is not bool
        or manifest.get("schema") != "sonoranlab.manifest/1"
        or not isinstance(manifest.get("inputs"), dict)
        or not isinstance(manifest.get("outputs"), dict)
        or not isinstance(manifest.get("profile"), dict)
        or not isinstance(manifest.get("character"), str)
        or not isinstance(manifest.get("result"), dict)
        or type(manifest["result"].get("pass")) is not bool
        or manifest["result"]["pass"] != report["pass"]
    ):
        raise ToolError("invalid_params", f"{job}: missing or corrupt report/manifest metadata")
    profile_path = manifest["profile"].get("path")
    if not isinstance(profile_path, str):
        raise ToolError("invalid_params", f"{job / 'manifest.json'}: missing profile path")
    profile_file = Path(profile_path)
    _protected(profile_file, ctx)
    if profile_file.resolve() not in {p.path.resolve() for p in ctx.profiles()}:
        paths.resolve_input(profile_file)
    require_regular_file(profile_file)
    for relative in manifest["outputs"]:
        _job_file(job, relative)
    for name in manifest["inputs"]:
        if not isinstance(name, str) or Path(name).name != name:
            raise ToolError("path_refused", "Raw input name must be a plain filename")
        _job_file(job, "raw/" + name)
    return job, manifest, animate


def _review(job: Path, ctx: Context) -> dict:
    from sonoranlab.review import production_usable, review_errors

    # Locate using the import evidence; callers never follow a newly edited animate pointer.
    manifest_path = job / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = _json(_job_file(job, "manifest.json"), code="invalid_params")
        except ToolError as exc:
            if exc.error["code"] != "invalid_params":
                raise
        else:
            profile = manifest.get("profile")
            if isinstance(profile, dict) and isinstance(profile.get("path"), str):
                path = Path(profile["path"])
                _protected(path, ctx)
                if path.resolve() not in {p.path.resolve() for p in ctx.profiles()}:
                    # Missing provenance is reported by the review layer as an invalid binding.
                    with suppress(FileNotFoundError):
                        paths.resolve_input(path)
    problems = review_errors(job)
    usable = production_usable(job)
    exists = (job / "review.json").exists() or (job / "review.json").is_symlink()
    state = "none" if not exists else "pass" if usable else "invalid"
    if exists and {p["code"] for p in problems} == {"REVIEW_NOT_PASS"}:
        state = "fail"
    return {"production_usable": usable, "review_state": state, "problems": problems}


def import_job(project: str, character: str, job_dir: str, *, ctx: Context) -> dict:
    profile = _character(project, character, ctx)
    job = _job_directory(job_dir, ctx)
    artifacts_root = paths.state_root() / "artifacts"
    if job.is_relative_to(artifacts_root) and not job.is_relative_to(artifacts_root / project / character):
        raise ToolError("path_refused", "Job belongs to another project or character")
    current, manifest, animate = _job_metadata(job, ctx)
    inputs = animate["inputs"] if animate else manifest
    if inputs.get("character") != character:
        raise ToolError("invalid_params", "Job character differs from requested character")
    provenance = inputs.get("profile")
    if not isinstance(provenance, dict) or not isinstance(provenance.get("path"), str):
        raise ToolError("invalid_params", "Job has no profile provenance")
    # A synthetic or historical profile copy must still identify the same project.
    from sonoranlab.profile import load_profile

    provenances = [provenance, manifest["profile"]] if manifest else [provenance]
    for binding in provenances:
        source_profile = Path(binding["path"])
        _protected(source_profile, ctx)
        if source_profile.resolve() != profile.path.resolve():
            paths.resolve_input(source_profile)
        require_regular_file(source_profile)
        if load_profile(source_profile).id != project or sha256_file(source_profile) != binding.get("sha256"):
            raise ToolError("invalid_params", "Job profile provenance does not match the requested project")
    if manifest and manifest.get("character") != character:
        raise ToolError("invalid_params", "Final job character differs from requested character")
    for relative, expected in manifest.get("outputs", {}).items():
        if (
            not isinstance(expected, str)
            or not _HASH.fullmatch(expected)
            or sha256_file(_job_file(current, relative)) != expected
        ):
            raise ToolError("invalid_params", f"{current / 'manifest.json'}: output hash mismatch for {relative}")
    from sonoranlab.artifacts import verify_raw

    if manifest and (problems := verify_raw(current, inputs=manifest["inputs"])):
        raise ToolError("invalid_params", f"{current}: raw provenance mismatch: {'; '.join(problems)}")
    hashes = {}
    for root, dirs, files in os.walk(job, followlinks=False):
        for name in dirs:
            if (Path(root) / name).is_symlink():
                raise ToolError("path_refused", "Symlink directory in imported job")
        for name in files:
            path = Path(root) / name
            try:
                require_regular_file(path, root=job)
            except ValueError as exc:
                raise ToolError("path_refused", str(exc)) from exc
            if path.name in {"report.json", "manifest.json", "animate.json", "review.json", "repair-manifest.json"}:
                _json(_job_file(job, path.relative_to(job).as_posix()), code="invalid_params")
                hashes[path.relative_to(job).as_posix()] = sha256_file(path)
            elif "export" in path.relative_to(job).parts:
                hashes[path.relative_to(job).as_posix()] = sha256_file(path)
    artifact_id = "import-" + hashlib.sha256(f"{project}/{character}/{job}".encode()).hexdigest()[:24]
    base = _base(project, character, ctx, create=True)
    with _lock(base):
        record = _record(base, project, character)
        old = next((j for j in record["jobs"] if j["artifact_id"] == artifact_id), None)
        if old:
            if old["hashes"] != hashes:
                raise ToolError("conflict", "Imported job changed; existing import evidence is immutable")
            return {"artifact_id": artifact_id, "job_id": old["job_id"], "created": False}
        job_id = (
            job.name
            if _JOB.fullmatch(job.name)
            else datetime.now(UTC).strftime("job-%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
        )
        record["spec"] = _spec(profile, character)
        record["jobs"].append(
            {
                "artifact_id": artifact_id,
                "job_id": job_id,
                "job_dir": str(job),
                "review_job": str(current),
                "hashes": hashes,
                "provenance": provenance,
                "imported_at": datetime.now(UTC).isoformat(),
            }
        )
        _atomic(base / "character.json", record)
    return {"artifact_id": artifact_id, "job_id": job_id, "created": True}


def _records(ctx: Context):
    root = _state(paths.state_root() / "registry", ctx)
    for path in sorted(root.glob("*/*/character.json")):
        base = _base(path.parent.parent.name, path.parent.name, ctx)
        yield base, _record(base, base.parent.name, base.name)


def _find_job(ref: str, ctx: Context) -> dict:
    if not (_JOB.fullmatch(ref) or re.fullmatch(r"import-[a-z0-9]+", ref)):
        raise ToolError("invalid_params", "Expected a job id or imported artifact id")
    matches = [
        job for _, record in _records(ctx) for job in record["jobs"] if ref in (job["artifact_id"], job["job_id"])
    ]
    if not matches:
        raise ToolError("not_found", "Unknown imported artifact or job id")
    if len(matches) > 1:
        raise ToolError("conflict", "Ambiguous job id; use the imported artifact id")
    return matches[0]


def check_review(job: str, *, ctx: Context) -> dict:
    entry = _find_job(job, ctx)
    return _review(_review_directory(entry, ctx), ctx)


def _review_directory(entry: dict, ctx: Context) -> Path:
    root = _job_directory(entry["job_dir"], ctx)
    current = _job_directory(entry["review_job"], ctx)
    if not current.is_relative_to(root):
        raise ToolError("path_refused", "Review job escapes imported job")
    return current


def _spec(profile, character: str) -> dict:
    spec = profile.character(character)
    return {
        "cell": list(spec.cell),
        "anchor_mode": spec.anchor_mode,
        "animations": json.loads(json.dumps({k: asdict(v) for k, v in spec.animations.items()}, default=str)),
    }


def _job_summary(entry: dict, ctx: Context) -> dict:
    job = _job_directory(entry["job_dir"], ctx)
    current = _review_directory(entry, ctx)
    try:
        _, manifest, animate = _job_metadata(job, ctx)
    except (ToolError, FileNotFoundError) as exc:
        if isinstance(exc, ToolError) and exc.error["code"] != "invalid_params":
            raise
        manifest, animate = {}, None
    review = _review(current, ctx)
    result = manifest.get("result", {})
    verdict = result.get("pass")
    godot = (animate or manifest).get("godot_check")
    gc = "not_run"
    if isinstance(godot, dict):
        gc = "pass" if godot.get("pass") is True else "unavailable" if godot.get("available") is False else "fail"
    links = manifest.get("repair") or manifest.get("upstream") or {}
    parent = links.get("parent_job") if isinstance(links, dict) else None
    return {
        **entry,
        "technical": {"validator": None if verdict is None else "pass" if verdict else "fail", "godot": gc},
        "human": {"review": review["review_state"], "production_usable": review["production_usable"]},
        "review_problems": review["problems"],
        "lineage": {"parent_job": parent, "repair_children": []},
        "current_hashes": {rel: _current_hash(job, rel) for rel in entry["hashes"]},
        "review_job": str(current),
    }


def get_character(project: str, character: str, *, ctx: Context) -> dict:
    profile = _profile(project, ctx)
    base = _base(project, character, ctx)
    record = _record(base, project, character)
    if character not in profile.characters and not (base / "character.json").exists():
        raise ToolError("not_found", "Unknown character", field="character")
    spec = _spec(profile, character) if character in profile.characters else record.get("spec")
    if not isinstance(spec, dict):
        raise ToolError("internal", f"{base / 'character.json'}: missing character spec")
    jobs = [_job_summary(job, ctx) for job in record["jobs"]]
    for job in jobs:
        job["lineage"]["repair_children"] = [
            child["job_id"] for child in jobs if child["lineage"]["parent_job"] in (job["job_dir"], job["review_job"])
        ]
    return {
        "id": character,
        **spec,
        "registered": bool(record["reference_sets"]),
        "reference_sets": [_verify_refs(base, refs, ctx) for refs in record["reference_sets"]],
        "jobs": jobs,
    }


def list_characters(project: str, *, ctx: Context) -> dict:
    profile = _profile(project, ctx)
    names = set(profile.characters)
    root = _state(paths.state_root() / "registry" / project, ctx)
    names.update(p.parent.name for p in root.glob("*/character.json"))
    characters = []
    for name in sorted(names):
        record = get_character(project, name, ctx=ctx)
        record["reference_sets"] = [
            {"ref_set_id": r["ref_set_id"], "approval": r["approval"]} for r in record["reference_sets"]
        ]
        record["jobs"] = [job["job_id"] for job in record["jobs"]]
        characters.append(record)
    return {"characters": characters}


def _current_hash(job: Path, relative: str) -> str | None:
    try:
        return sha256_file(_job_file(job, relative))
    except FileNotFoundError:
        return None


def get_artifact(ref: str, *, ctx: Context) -> dict:
    import mimetypes

    from PIL import Image

    identifier, separator, relative = ref.partition(":")
    entry = _find_job(identifier, ctx)
    job = _job_directory(entry["job_dir"], ctx)
    if not separator:
        # Imported-id metadata is bounded: file count/digest, never an unbounded manifest or pixel data.
        return {
            "artifact_id": entry["artifact_id"],
            "job_id": entry["job_id"],
            "file_count": len(entry["hashes"]),
            "size": sum(_job_file(job, relative).stat().st_size for relative in entry["hashes"]),
            "sha256": hashlib.sha256(json.dumps(entry["hashes"], sort_keys=True).encode()).hexdigest(),
            "media_type": "application/vnd.sonoranlab.import+json",
        }
    path = _job_file(job, relative)
    _protected(path, ctx)
    result = {
        "ref": f"{entry['job_id']}:{relative}",
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    }
    if path.suffix.lower() == ".png":
        with Image.open(path) as image:
            result["dimensions"] = list(image.size)
    return result
