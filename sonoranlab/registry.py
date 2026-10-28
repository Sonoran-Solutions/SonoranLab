"""Small, locked registry records; approval and human review are always verified on read."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from contextlib import contextmanager, suppress
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import yaml
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
    approval_problems = [*problems]
    latest_valid = False
    approvals = _state(base / "approvals", ctx)
    # Filename order is the owner writer's timestamp order. A new valid approval
    # supersedes earlier invalid evidence; retain every historical problem for inspection.
    for path in sorted(approvals.glob("*.json")):
        try:
            approval = _json(path)
            if approval.get("ref_set_id") not in (None, refs["ref_set_id"]):
                continue
            evidence.append(
                {
                    "path": path.relative_to(paths.state_root()).as_posix(),
                    "sha256": sha256_file(path),
                    "record": approval,
                }
            )
            latest_valid = (
                approval.get("ref_set_id") == refs["ref_set_id"]
                and approval.get("schema") == "sonoranlab.ref-approval/1"
                and approval.get("project") == base.parent.name
                and approval.get("character") == base.name
                and approval.get("hashes") == current
                and all(
                    isinstance(approval.get(k), str) and approval[k].strip()
                    for k in ("reviewer", "evidence", "approved_at")
                )
            )
            if not latest_valid:
                approval_problems.append(f"{path.name}: invalid approval binding or metadata")
        except (ToolError, OSError, ValueError) as exc:
            latest_valid = False
            approval_problems.append(f"{path.name}: unreadable approval ({exc})")
    return {
        **refs,
        "verification": {"hashes": current, "problems": problems},
        "approval": {
            "state": "invalid"
            if problems or (approval_problems and not latest_valid)
            else "approved"
            if latest_valid
            else "unapproved",
            "evidence": evidence,
            "problems": approval_problems,
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
        created = False
        try:
            destination.mkdir()
            created = True
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
            refs = {
                "ref_set_id": ref_id,
                "files": files,
                "registered_at": datetime.now(UTC).isoformat(),
                "notes": notes,
            }
            record["spec"] = _spec(profile, character)
            record["reference_sets"].append(refs)
            _atomic(base / "character.json", record)
        except BaseException:
            # Only remove the set this call created, and never discard a committed record's evidence.
            if not (base / "character.json").exists():
                if created:
                    destination.chmod(0o755)
                    shutil.rmtree(destination)
                with suppress(OSError):
                    parent.rmdir()
                _sync_dir(base)
            raise

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
            job = _animate_final(animate, job, ctx)
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


def _review(job: Path, ctx: Context, *, entry: dict | None = None) -> dict:
    from sonoranlab.review import production_usable, review_errors

    if entry is not None:
        root = _job_directory(entry["job_dir"], ctx)
        recorded = entry["hashes"]
        drift = []
        for relative, expected in recorded.items():
            if Path(relative).name == "review.json":
                continue  # The review layer checks current owner evidence, which may be recorded after import.
            if _current_hash(root, relative) != expected:
                drift.append(relative)
        for path in root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if (
                "export" in Path(relative).parts
                and relative not in recorded
                and (not path.is_dir() or path.is_symlink())
            ):
                drift.append(relative)
        review_relative = job.relative_to(root)
        if any((review_relative / name).as_posix() not in recorded for name in ("report.json", "manifest.json")):
            drift.append("report/manifest absent at import")
        if drift:
            return {
                "production_usable": False,
                "review_state": "invalid",
                "problems": [{"code": "import_drift", "message": "Import snapshot differs: " + ", ".join(drift)}],
            }

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


def _confine_profile_templates(source: Path) -> None:
    """Preflight the loader's indirect reads, including overridden character animations.

    ponytail: load_profile(check_paths=False) only reads template.yaml indirectly;
    extend this preflight if the loader gains other referenced-file reads.
    """
    try:
        raw = yaml.safe_load(source.read_text())
    except yaml.YAMLError as exc:
        raise ToolError("invalid_params", "Job profile is not valid YAML") from exc
    if not isinstance(raw, dict):
        raise ToolError("invalid_params", "Job profile must be a mapping")
    characters = raw.get("characters")
    sections = [raw, *(characters.values() if isinstance(characters, dict) else [])]
    for section in sections:
        animations = section.get("animations") if isinstance(section, dict) else None
        for anim in animations.values() if isinstance(animations, dict) else []:
            template = anim.get("template") if isinstance(anim, dict) else None
            if isinstance(template, str):
                # Profiles use ../templates/... relative to their own directory; normalise lexically, then confine
                # (paths._confine still checks both the lexical and the symlink-resolved path against the roots).
                path = Path(os.path.normpath(source.resolve().parent / Path(template).expanduser() / "template.yaml"))
                paths.resolve_input(path)
                require_regular_file(path)


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
        require_regular_file(source_profile)
        if source_profile.resolve() != profile.path.resolve():
            paths.resolve_input(source_profile)
            _confine_profile_templates(source_profile)
        # The repository profile (including its templates) was already loaded by _character.
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
    if native := _native_job(job):
        return _native_review(native, ctx)
    entry = _find_job(job, ctx)
    return _review(_review_directory(entry, ctx), ctx, entry=entry)


def _native_job(identifier: str) -> dict | None:
    from sonoranlab.jobs import manager, store

    if not _JOB.fullmatch(identifier):
        return None
    path = paths.state_root() / "jobs" / identifier
    if not (path.exists() or path.is_symlink()):
        return None
    job = manager.get(identifier)
    if manager.corrupt(job):
        raise ToolError("refused", "Job record is corrupt; preserve it")
    # store.output rejects STATE symlinks and escaping output_dir records.
    store.output(job)
    return job


def _animate_final(doc: dict, root: Path, ctx: Context) -> Path:
    final = _job_directory((doc.get("verdict") or {}).get("job", root), ctx)
    if not final.is_relative_to(root.resolve()):
        raise ToolError("path_refused", "Animate final job escapes its directory")
    return final


def _native_review(job: dict, ctx: Context) -> dict:
    from sonoranlab.jobs import store

    root = store.output(job)
    if job["kind"] == "animate" and (root / "animate/animate.json").is_file():
        doc = _json(_job_file(root, "animate/animate.json"), code="invalid_params")
        root = _animate_final(doc, root / "animate", ctx)
    elif job["kind"] == "finalize_repair" and (job.get("result") or {}).get("artifact_id"):
        return check_review(job["result"]["artifact_id"], ctx=ctx)
    return _review(root, ctx)


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
    try:
        job = _job_directory(entry["job_dir"], ctx)
        current = _review_directory(entry, ctx)
        review = _review(current, ctx, entry=entry)
        manifest, animate = {}, None
        if not any(p["code"] == "import_drift" for p in review["problems"]):
            _, manifest, animate = _job_metadata(job, ctx)
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
    except (ToolError, OSError, ValueError) as exc:
        problem = (
            {"code": exc.error["code"], "message": exc.error["message"]}
            if isinstance(exc, ToolError)
            else {"code": "invalid_params" if isinstance(exc, ValueError) else "internal", "message": str(exc)}
        )
        return {
            **entry,
            "technical": {"validator": "invalid", "godot": "invalid"},
            "human": {"review": "invalid", "production_usable": False},
            "review_problems": [problem],
            "lineage": {"parent_job": None, "repair_children": []},
            "current_hashes": {},
        }


def _header(project: str, character: str, ctx: Context) -> tuple[Path, dict, dict]:
    profile = _profile(project, ctx)
    base = _base(project, character, ctx)
    record = _record(base, project, character)
    if character not in profile.characters and not (base / "character.json").exists():
        raise ToolError("not_found", "Unknown character", field="character")
    spec = _spec(profile, character) if character in profile.characters else record.get("spec")
    if not isinstance(spec, dict):
        raise ToolError("internal", f"{base / 'character.json'}: missing character spec")
    header = {
        "id": character,
        **spec,
        "registered": bool(record["reference_sets"]),
        "reference_sets": [_verify_refs(base, refs, ctx) for refs in record["reference_sets"]],
    }
    return base, record, header


def _resolved(value) -> Path | None:
    try:
        return Path(value).resolve() if isinstance(value, str) and value else None
    except (OSError, RuntimeError, ValueError):
        return None


def _native_records() -> list[tuple[str, dict, dict | None]]:
    """Pure reads of every durable record (no lock, no reconciliation): (job_id, record, raw corrupt JSON)."""
    from sonoranlab.jobs import manager, store

    try:
        root = store.directory("jobs")
    except FileNotFoundError:
        return []
    found = []
    for path in sorted(root.iterdir()):
        if not (re.fullmatch(store.JOB_ID, path.name) and path.is_dir() and not path.is_symlink()):
            continue
        job, raw = store.read(path), None
        if manager.corrupt(job):
            # Best-effort attribution only; the corrupt record itself is never repaired or rewritten.
            with suppress(OSError, ValueError):
                raw = store.load(path / "job.json")
        found.append((path.name, job, raw if isinstance(raw, dict) else None))
    return found


def _history(project: str, character: str, record: dict, natives, ctx: Context, *, lineage: bool) -> tuple[list, int]:
    """Merge durable jobs and imports by exact (project, character) and exact resolved directory identity.

    An import is the same logical job as a native one when its resolved job_dir equals or lies inside the native
    output_dir, or when it is the child a finalize_repair job imported (same artifact_id, a hash of the exact path).
    Returns (sorted logical items, unattributed corrupt record count). No reviews, hashes or reconciliation here.
    """
    from sonoranlab.jobs import manager

    state, items, unattributed = paths.state_root(), [], 0
    for job_id, job, raw in natives:
        source = raw if manager.corrupt(job) else job
        if source is None:
            unattributed += 1
            continue
        if (source.get("project"), source.get("character")) != (project, character):
            continue
        # A corrupt record is listed (invalid) but its untrusted output_dir never claims imports.
        output = None if source is raw else source["output_dir"]
        created = source.get("created_at")
        items.append(
            {
                "id": job_id,
                "native": job,
                "imports": [],
                "created_at": created if isinstance(created, str) else "",
                "dirs": [d for d in [_resolved(str(state / output)) if output else None] if d],
            }
        )
    for entry in record["jobs"]:
        directory = _resolved(entry["job_dir"])
        owner = next(
            (
                item
                for item in items
                if item["native"]
                and item["native"].get("kind") == "finalize_repair"
                and ((item["native"].get("result") or {}).get("artifact_id") == entry["artifact_id"])
            ),
            None,
        ) or next(
            (item for item in items if directory and any(directory.is_relative_to(d) for d in item["dirs"])), None
        )
        if owner is None:
            created = entry.get("imported_at")
            owner = {
                "id": entry["artifact_id"],
                "native": None,
                "imports": [],
                "created_at": created if isinstance(created, str) else "",
                "dirs": [],
            }
            items.append(owner)
        owner["imports"].append(entry)
        owner["dirs"] += [d for d in (directory, _resolved(entry["review_job"])) if d]
    items.sort(key=lambda item: (item["created_at"], item["id"]))
    for item in items:
        native = item["native"] if item["native"] and not manager.corrupt(item["native"]) else None
        item["parent_job_id"] = native["parent_job_id"] if native else None
        item["parent_job"] = (
            next(filter(None, (_repair_parent(e, ctx) for e in item["imports"])), None) if lineage else None
        )
    for item in items:
        item["retries"] = [i["id"] for i in items if i["parent_job_id"] == item["id"]]
        item["parent_id"] = None
        if parent := _resolved(item["parent_job"]):
            # The most specific containing job directory names the repair parent.
            best = max(
                (
                    (len(d.parts), other["id"])
                    for other in items
                    if other is not item
                    for d in other["dirs"]
                    if parent.is_relative_to(d)
                ),
                default=None,
            )
            item["parent_id"] = best[1] if best else None
    for item in items:
        item["children"] = [i for i in items if i["parent_id"] == item["id"]]
    return items, unattributed


def _repair_parent(entry: dict, ctx: Context) -> str | None:
    try:
        manifest = _json(_job_file(_review_directory(entry, ctx), "manifest.json"), code="invalid_params")
    except (ToolError, OSError, ValueError):
        return None
    links = manifest.get("repair") or manifest.get("upstream") or {}
    parent = links.get("parent_job") if isinstance(links, dict) else None
    return parent if isinstance(parent, str) else None


_REVIEW_RANK = ("pass", "none", "fail", "invalid")


def _history_entry(item: dict, ctx: Context) -> dict:
    """Recompute one logical job's verdicts from current files; failures stay inside this entry."""
    from sonoranlab.api.tools.jobs import measured_progress
    from sonoranlab.jobs import manager

    imports = [_job_summary(entry, ctx) for entry in item["imports"]]
    problems = []
    for summary in imports:
        if summary["technical"]["validator"] == "invalid" or any(
            p["code"] == "import_drift" for p in summary["review_problems"]
        ):
            problems += [{**p, "source": summary["artifact_id"]} for p in summary["review_problems"]]
    entry = {**imports[0]} if imports else {}  # Import-backed entries keep the DYL-114 import fields.
    ids = {item["id"]} | {i["artifact_id"] for i in imports} | {i["job_id"] for i in imports}
    entry.update(
        id=item["id"],
        aliases=sorted(ids - {item["id"]}),
        source="native+import" if item["native"] and imports else "native" if item["native"] else "import",
        kind="import",
        anim=None,
        created_at=item["created_at"] or None,
        state=None,
        stage=None,
        error=None,
        attempt={"number": None, "parent_job_id": None, "retries": item["retries"]},
        imports=[
            {
                k: i.get(k)
                for k in ("artifact_id", "job_id", "job_dir", "review_job", "imported_at", "technical", "human")
            }
            for i in imports
        ],
        lineage={
            "parent_job": item["parent_job"],
            "parent_id": item["parent_id"],
            # DYL-114 callers read repair_children as each child's job_id.
            "repair_children": [c["id"] if c["native"] else c["imports"][0]["job_id"] for c in item["children"]],
            "child_ids": [child["id"] for child in item["children"]],
        },
    )
    reviews = [i["human"] for i in imports]
    technical = imports[0]["technical"] if imports else None
    if item["native"] is not None:
        entry["job_id"] = item["id"]
        try:
            job = manager.get(item["id"])  # The same reconciliation list_jobs/job_status already perform.
            if manager.corrupt(job):
                raise ToolError("internal", job["error"]["message"])
            entry.update(
                {key: job[key] for key in ("kind", "anim", "state", "stage", "error", "output_dir")},
                attempt={"number": job["attempt"], "parent_job_id": job["parent_job_id"], "retries": item["retries"]},
            )
            measured = measured_progress(job).get("technical") or (job.get("result") or {}).get("technical")
            if measured and (measured.get("validator") is not None or measured.get("godot") != "not_run"):
                technical = measured
            technical = technical or measured or {"validator": None, "godot": "not_run"}
            review = _native_review(job, ctx)
            reviews.append({"review": review["review_state"], "production_usable": review["production_usable"]})
            if not imports:
                entry["review_problems"] = review["problems"]
        except (ToolError, OSError, ValueError, KeyError, TypeError) as exc:
            code = exc.error["code"] if isinstance(exc, ToolError) else "internal"
            message = exc.error["message"] if isinstance(exc, ToolError) else str(exc)
            problems.append({"code": code, "message": message, "source": item["id"]})
            technical = {"validator": "invalid", "godot": "invalid"}
            reviews.append({"review": "invalid", "production_usable": False})
            entry.setdefault("review_problems", [{"code": code, "message": message}])
    # Every bound review must agree: the most conservative state wins and approval needs all of them.
    entry["technical"] = technical
    entry["human"] = {
        "review": max((r["review"] for r in reviews), key=_REVIEW_RANK.index),
        "production_usable": all(r["production_usable"] for r in reviews),
    }
    entry["status"] = "invalid" if problems else "ok"
    entry["problems"] = problems
    return entry


def get_character(project: str, character: str, *, jobs_offset: int = 0, jobs_limit: int = 50, ctx: Context) -> dict:
    _, record, header = _header(project, character, ctx)
    items, unattributed = _history(project, character, record, _native_records(), ctx, lineage=True)
    page = items[jobs_offset : jobs_offset + jobs_limit]
    return {
        **header,
        "jobs": [_history_entry(item, ctx) for item in page],
        "job_count": len(items),
        "jobs_offset": jobs_offset,
        "jobs_truncated": jobs_offset + len(page) < len(items),
        "unattributed_invalid_jobs": unattributed,
    }


def list_characters(project: str, *, ctx: Context) -> dict:
    profile = _profile(project, ctx)
    names = set(profile.characters)
    root = _state(paths.state_root() / "registry" / project, ctx)
    names.update(p.parent.name for p in root.glob("*/character.json"))
    natives = _native_records()
    characters = []
    for name in sorted(names):
        _, record, header = _header(project, name, ctx)
        header["reference_sets"] = [
            {"ref_set_id": r["ref_set_id"], "approval": r["approval"]} for r in header["reference_sets"]
        ]
        # Lightweight: imported IDs from the record and a merged history count; no manifests or reviews.
        header["jobs"] = [job["job_id"] for job in record["jobs"]]
        header["job_count"] = len(_history(project, name, record, natives, ctx, lineage=False)[0])
        characters.append(header)
    return {"characters": characters}


def _current_hash(job: Path, relative: str) -> str | None:
    try:
        return sha256_file(_job_file(job, relative))
    except (ToolError, OSError, ValueError):
        return None


def get_artifact(ref: str, *, ctx: Context) -> dict:
    import mimetypes

    from PIL import Image

    identifier, separator, relative = ref.partition(":")
    native = _native_job(identifier)
    if native:
        from sonoranlab.jobs import store

        job = _job_directory(store.output(native), ctx)
        entry = {"job_id": identifier}
    else:
        entry = _find_job(identifier, ctx)
        job = _job_directory(entry["job_dir"], ctx)
    if not separator:
        if native:
            files = [
                _job_file(job, p.relative_to(job).as_posix())
                for p in job.rglob("*")
                if not p.is_dir() or p.is_symlink()
            ]
            return {
                "job_id": identifier,
                "file_count": len(files),
                "size": sum(p.stat().st_size for p in files),
                "media_type": "application/vnd.sonoranlab.job+json",
            }
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
