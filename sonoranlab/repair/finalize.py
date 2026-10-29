"""Finalize saved pixels into a new, independently validated and reviewed child job.

Destination checks and fd-based reservation defend against mistaken or hostile
paths and symlinks present at check time. They do not defend against a concurrent
same-UID process renaming or swapping ancestors during the command; it can write
there directly anyway. Final verification detects interference best-effort,
without guaranteeing that path-based writes stayed in staging.

ponytail: Run prepare/finalize inside a mount namespace (bubblewrap) with only the
staging root bound writable and the game checkout and repository not mounted to
isolate writes from concurrent ancestor swaps.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

from sonoranlab import artifacts
from sonoranlab.contact import contact_sheet
from sonoranlab.export.godot import export_godot
from sonoranlab.pipeline import PipelineError, check_in_godot
from sonoranlab.profile import load_palette, load_profile
from sonoranlab.repair import EditorUnavailable, Refused
from sonoranlab.repair.authorization import lease, require, safe_tree
from sonoranlab.repair.authorization import validate as validate_authorization
from sonoranlab.repair.broker import Broker, Journal, Snapshot
from sonoranlab.repair.compare import (
    changed_coordinates,
    compare,
    expected_mapping,
    load_rgba,
    pixel_problems,
    png_chunks,
    read_json,
    source_name,
)
from sonoranlab.repair.confine import (
    admit_repair,
    check_destination,
    create_destination,
    git_root,
    repair_root,
    verify_destination,
)
from sonoranlab.repair.runtime import AsepriteConverter, no_symlinks
from sonoranlab.slice import pack_sheet, slice_sheet
from sonoranlab.validate import read_frames, read_stills, validate


def _journal(repair: Path, state: dict) -> dict:
    """Reuse the broker's chain verifier without appending or updating session state."""
    require(not state.get("outcome_unknown"), "outcome_unknown")
    path = no_symlinks(repair / "session/ops.jsonl")
    if not path.exists():
        require(not state.get("log_sha256"), "log_corrupt")
        return {"sha256": None, "operations": [], "had_session": False}
    try:
        journal = Journal(path, state, repair / "state.json")
        try:
            require(journal.last == state.get("log_sha256"), "log_corrupt")
        finally:
            journal.close()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
    except (ValueError, KeyError, TypeError) as e:
        from sonoranlab.repair.authorization import Denied

        raise Denied("log_corrupt", str(e)) from e
    operations, pending = {}, set()
    for row in rows:
        ident = row.get("op_id")
        request = row.get("request", {})
        if isinstance(request, dict) and request.get("action") == "op":
            ident = ident or request.get("op_id")
            if ident:
                operations.setdefault(
                    ident,
                    {
                        "op_id": ident,
                        "op": request.get("op"),
                        "target": request.get("target"),
                        "verdict": "not_dispatched",
                        "changed_count": 0,
                    },
                )
        if ident and row["event"] in ("dispatch", "outcome_unknown"):
            pending.add(ident)
        if ident and row["event"] == "verification":
            response = row.get("response", {})
            if response.get("outcome") in ("unchanged", "applied_within_mask") or response.get("restored") is True:
                pending.discard(ident)
        if ident in operations and "response" in row:
            response = row["response"]
            operations[ident].update(
                verdict=response.get(
                    "outcome", response.get("error", "accepted" if response.get("ok") else "rejected")
                ),
                changed_count=sum(len(v) for v in response.get("changed", {}).values() if isinstance(v, list)),
            )
    require(not pending, "outcome_unknown")
    return {
        "sha256": artifacts.sha256_file(path),
        "operations": list(operations.values()),
        "had_session": any(row["event"] == "session_start" for row in rows),
    }


def _sources(repair: Path, doc: dict) -> dict[str, np.ndarray]:
    """Decode the same bytes whose hashes were verified, including for review evidence."""
    source = repair / "source"
    require({p.name for p in source.iterdir()} == set(doc["source"]), "source_hash_mismatch")
    result = {}
    for name, digest in doc["source"].items():
        data = no_symlinks(source / name).read_bytes()
        require(hashlib.sha256(data).hexdigest() == digest, "source_hash_mismatch")
        with Image.open(BytesIO(data)) as image:
            result[name] = np.asarray(image.convert("RGBA")).copy()
    return result


def _verify(repair: Path) -> tuple:
    doc = read_json(repair / "repair.json")
    require(doc.get("schema") == "sonoranlab.repair/1", "invalid_repair")
    parent = no_symlinks(Path(doc["parent_job"]))
    profile_path = no_symlinks(Path(doc["profile"]["path"]))
    require(
        profile_path.is_file() and artifacts.sha256_file(profile_path) == doc["profile"]["sha256"],
        "profile_hash_mismatch",
    )
    profile = load_profile(profile_path)
    safe_tree(repair / "source")
    safe_tree(repair / "workspace")
    require((repair / "workspace/doc.aseprite").is_file(), "workspace_mismatch")
    source = repair / "source"
    mapping = expected_mapping(doc)
    require(read_json(repair / "mapping.json") == mapping, "mapping_mismatch")
    require(set(doc["source"]) == {source_name(e) for e in mapping.values()}, "source_hash_mismatch")
    require({p.name for p in source.iterdir()} == set(doc["source"]), "source_hash_mismatch")
    for name, digest in doc["source"].items():
        require(artifacts.sha256_file(source / name) == digest, "source_hash_mismatch")
    for name, digest in doc["parent_hashes"].items():
        path = no_symlinks(parent / name)
        require(path.is_relative_to(parent) and path.is_file(), "parent_hash_mismatch")
        require(artifacts.sha256_file(path) == digest, "parent_hash_mismatch")
    exports = {p.relative_to(parent).as_posix() for p in (parent / "export").rglob("*") if p.is_file()}
    require(exports == {k for k in doc["parent_hashes"] if k.startswith("export/")}, "parent_hash_mismatch")
    require({"report.json", "manifest.json"} <= set(doc["parent_hashes"]), "parent_hash_mismatch")
    manifest = read_json(parent / "manifest.json")
    require(doc["profile"]["sha256"] == manifest.get("profile", {}).get("sha256"), "profile_hash_mismatch")
    outputs = manifest.get("outputs", {})
    require(exports == {k for k in outputs if k.startswith("export/")}, "parent_hash_mismatch")
    require(all(artifacts.sha256_file(parent / name) == outputs[name] for name in exports), "parent_hash_mismatch")
    require((parent / "review.json").exists() == ("review.json" in doc["parent_hashes"]), "parent_hash_mismatch")
    palette = load_palette(profile)  # verifies the palette's own profile-bound sha256
    require(["#" + bytes(c).hex() for c in palette] == doc["palette"], "profile_mismatch")
    spec = profile.character(doc["character"])
    clip = spec.animations[doc["anim"]]
    require(
        list(spec.cell) == doc["cell"]
        and clip.frames == doc["frames"]
        and clip.fps == doc["fps"]
        and clip.loop == doc["loop"]
        and 1000 / clip.fps == doc["duration_ms"]
        and doc["directions"]
        == {
            "order": list(profile.directions.order),
            "generate": list(profile.directions.generate),
            "mirror": dict(profile.directions.mirror_animations),
        },
        "profile_mismatch",
    )
    state = read_json(repair / "state.json")
    require("TAINTED" not in (state.get("state"), state.get("session_state")), "tainted")
    auth_path = no_symlinks(repair / "authorization.json")
    require(auth_path.is_file(), "authorization_missing")
    require(artifacts.sha256_file(auth_path) == state.get("authorization_sha256"), "authorization_hash_mismatch")
    auth = read_json(auth_path)
    masks = validate_authorization(auth, doc, mapping)
    require(
        type(state.get("iterations", 0)) is int and 0 <= state.get("iterations", 0) <= auth["max_iterations"],
        "iteration_limit",
    )
    operations = _journal(repair, state)
    require(state.get("state") in ("prepared", "checkpointed"), "invalid_state")
    had_session = (
        operations["had_session"]
        or any(key in state for key in ("session_state", "accepted_sha256", "document_sha256"))
        or state["state"] == "checkpointed"
    )
    accepted_hashes = None
    if had_session:
        require(state.get("session_state") == "STOPPED", "session_not_stopped")
        require(state["state"] == "checkpointed", "checkpoint_required")
        accepted = no_symlinks(repair / "session/accepted.json")
        require(
            accepted.is_file() and artifacts.sha256_file(accepted) == state.get("accepted_sha256"),
            "accepted_hash_mismatch",
        )
        accepted_hashes = Snapshot.unpack(read_json(accepted)).hashes()
    document_sha256 = state.get("document_sha256") if had_session else doc.get("document_sha256")
    require(
        artifacts.sha256_file(repair / "workspace/doc.aseprite") == document_sha256,
        "document_hash_mismatch",
    )
    for target in auth["targets"]:
        require(
            target["kind"] != "anim"
            or target["direction"] not in doc["directions"]["mirror"].values()
            or target["derive_mirror"],
            "mirror_derivation_required",
        )
    return doc, profile, auth, masks, operations, read_json(parent / "report.json"), accepted_hashes, document_sha256


def _export(repair: Path, converter: object, mapping: dict) -> Path:
    # workspace.export admits only `prepared`; finalize also admits saved checkpoints.
    workspace = repair / "workspace"
    n = 1
    while True:
        output = workspace / ("export" if n == 1 else f"export-{n:04d}")
        try:
            output.mkdir()
            break
        except FileExistsError:
            n += 1
    converter.export(workspace, output)
    safe_tree(output)
    meta = read_json(output / "meta.json")
    meta["png_chunks"] = {p.name: png_chunks(p) for p in output.glob("*.png")}
    (output / "meta.json").write_text(json.dumps(meta, sort_keys=True, indent=1) + "\n")
    artifacts.write_json(output, "mapping.json", mapping)
    mapped = output / "mapped"
    mapped.mkdir()
    for n, entry in mapping.items():
        if (output / f"{n}.png").is_file():
            shutil.copyfile(output / f"{n}.png", mapped / source_name(entry))
    artifacts.write_json(
        output,
        "export.json",
        {
            "doc_sha256": artifacts.sha256_file(workspace / "doc.aseprite"),
            "files": {
                p.relative_to(output).as_posix(): artifacts.sha256_file(p) for p in output.rglob("*") if p.is_file()
            },
        },
    )
    return output


def _measurements(parent: dict, child: dict) -> dict:
    """Numeric per-cell and cycle deltas, without inventing missing parent measurements."""
    result = {}
    for section, keys in (("frames", ("direction", "frame")), ("stills", ("direction",))):
        before = {tuple(r[k] for k in keys): r for r in parent.get(section, [])}
        for row in child.get(section, []):
            key = tuple(row[k] for k in keys)
            old = before.get(key, {})
            result[f"{section}/{'/'.join(map(str, key))}"] = {
                k: v - old[k] for k, v in row.items() if type(v) in (int, float) and type(old.get(k)) in (int, float)
            }
    result["cycle"] = {
        k: v - parent["cycle"][k]
        for k, v in child.get("cycle", {}).items()
        if type(v) in (int, float) and type(parent.get("cycle", {}).get(k)) in (int, float)
    }
    return result


def finalize(repair: Path, out: Path, *, converter: object | None = None) -> dict:
    """Return a JSON result; preflight refusals raise Refused before creating the child."""
    started = time.perf_counter()
    repair = admit_repair(repair)
    repair = no_symlinks(repair)
    # Refuse destinations before even creating a lease; reverify inputs under the lease below.
    doc = read_json(repair / "repair.json")
    profile = load_profile(no_symlinks(Path(doc["profile"]["path"])))
    palette_checkout = git_root(profile.palette.source)
    root = repair_root()
    forbidden = (
        Path(doc["parent_job"]),
        repair,
        profile.export.godot_project,
        *((palette_checkout,) if palette_checkout else ()),
    )
    out = check_destination(out, root=root, forbidden=forbidden)
    with lease(repair), ExitStack() as cleanup:  # held through publication
        require(not out.exists() and not out.is_symlink(), "output_exists")  # re-check under the lease
        doc, profile, auth, masks, operations, parent_report, accepted_hashes, document_sha256 = _verify(repair)
        mapping = expected_mapping(doc)
        source_cells = _sources(repair, doc)
        expected_pixels = (
            accepted_hashes
            if accepted_hashes is not None
            else {n: Broker.pixel_hash(source_cells[source_name(e)].copy()) for n, e in mapping.items()}
        )
        errors, comparison, exported, runtime = [], {}, None, None
        compared_hashes, colour_chunks = {}, {}
        try:
            converter = converter or AsepriteConverter()
            runtime = converter.record()
            exported = _export(repair, converter, mapping)
            compared_hashes = {
                source_name(e): artifacts.sha256_file(exported / f"{n}.png")
                for n, e in mapping.items()
                if (exported / f"{n}.png").is_file()
            }
            colour_chunks = {p.name: chunks for p in exported.glob("*.png") if (chunks := png_chunks(p))}
            if colour_chunks:
                errors.append({"code": "colour_chunks", "files": colour_chunks})
            comparison = compare(repair, exported)
            if comparison["metadata"]:
                errors.append({"code": "metadata_mismatch", "details": comparison["metadata"]})
            if {p.name for p in exported.glob("*.png")} != {f"{n}.png" for n in mapping}:
                errors.append({"code": "frame_set_mismatch"})
            for n, frame in comparison["frames"].items():
                if not frame["canvas_matches"]:
                    errors.append({"code": "canvas_mismatch", "frame": n})
                if frame["invalid_alpha"] or frame["off_palette"]:
                    errors.append({"code": "invalid_pixels", "frame": n})
                if any(n not in masks or not masks[n][y, x] for x, y in frame["changed"]):
                    errors.append({"code": "unauthorized_pixels", "frame": n, "changed": frame["changed"]})
            pixels = {n: Broker.pixel_hash(load_rgba(exported / f"{n}.png")) for n in comparison["frames"]}
            if pixels != expected_pixels:
                errors.append({"code": "checkpoint_mismatch" if accepted_hashes is not None else "session_required"})
        except (OSError, ValueError, Refused, subprocess.SubprocessError) as e:
            errors.append(
                {
                    "code": "editor_unavailable" if isinstance(e, EditorUnavailable) else "export_failed",
                    "message": str(e),
                }
            )
        exported_at = time.perf_counter()
        out, destination = create_destination(out, root=root, kind="child", id=doc["repair_id"], forbidden=forbidden)
        cleanup.callback(os.close, destination)
        job = out
        (job / "raw").mkdir()
        (job / "processed").mkdir()
        # Numbered PNGs were compared; mapped/ is only a disposable convenience copy.
        inputs = artifacts.ingest_raw(
            job,
            {source_name(e): exported / f"{n}.png" for n, e in mapping.items() if (exported / f"{n}.png").is_file()}
            if exported and not colour_chunks
            else {},
        )
        if inputs != compared_hashes:
            errors.append({"code": "compared_files_changed"})
        spec, anim = profile.character(doc["character"]), doc["anim"]
        clip = spec.animations[anim]
        order, gen, mirror = doc["directions"]["order"], doc["directions"]["generate"], doc["directions"]["mirror"]
        clips, stills = {}, {}
        try:
            clips, _ = read_frames(job / "raw", order)
            stills, _ = read_stills(job / "raw", order)
            rep = validate(
                clips,
                stills=stills,
                cell=spec.cell,
                frames=clip.frames,
                directions=gen,
                palette=load_palette(profile),
                foot_y=spec.foot_y,
                anchor_x=spec.anchor_x,
                padding=spec.padding,
                limits=spec.limits,
                max_colours=profile.palette.max_colors_per_cell,
                loop=clip.loop,
                anchor_mode=spec.anchor_mode,
                validator_mode=clip.validator_mode,
                fringe_tolerance=profile.alpha.fringe_tolerance,
                mirror=mirror,
                registration=parent_report.get("registration"),
                limits_source=spec.limits_source,
            )
        except (ValueError, OSError) as e:
            rep = {"pass": False, "failures": [str(e)], "warnings": []}
        validator = dict(rep)
        if not rep["pass"]:
            errors.append({"code": "validation_failed", "failures": rep["failures"]})
        params = {
            "repair_id": doc["repair_id"],
            "processing": "repair-finalize",
            "limits": vars(spec.limits),
            "limits_source": spec.limits_source,
            "directions": doc["directions"],
        }
        provenance = {
            "profile": doc["profile"],
            "inputs": inputs,
            "parent_job": doc["parent_job"],
            "repair_id": doc["repair_id"],
        }
        rep = {
            **rep,
            "character": spec.name,
            "animation": anim,
            "params": params,
            "provenance": provenance,
            "repair_errors": [],
        }
        gc, export = None, None
        if not errors:
            try:
                packed = pack_sheet(clips, spec.cell, order, clip.frames, mirror=mirror, stills=stills, required=order)
                export_clips = [{"name": anim, "start": 1, "frames": clip.frames, "fps": clip.fps, "loop": clip.loop}]
                if clip.loop_from is not None:
                    export_clips[0]["loop_from"] = clip.loop_from
                gc = check_in_godot(packed, character=spec.name, cell=spec.cell, rows=order, clips=export_clips)
                if not gc["pass"]:
                    errors.append({"code": "godot_failed", "result": gc})
                else:
                    # Stage the guarded export until its PNG/.tres match exactly what Godot loaded.
                    report_sha = hashlib.sha256((json.dumps(rep, sort_keys=True, indent=1) + "\n").encode()).hexdigest()
                    with tempfile.TemporaryDirectory(prefix="sonoranlab-finalize-") as tmp:
                        staged = export_godot(
                            packed,
                            character=spec.name,
                            cell=spec.cell,
                            rows=order,
                            clips=export_clips,
                            out_dir=Path(tmp),
                            provenance={**provenance, "report_sha256": report_sha},
                            forbid_under=profile.export.godot_project,
                        )
                        checked = gc["checked_sha256"]
                        actual = {Path(staged[k]).name: artifacts.sha256_file(Path(staged[k])) for k in ("png", "tres")}
                        if actual != checked:
                            errors.append({"code": "godot_hash_mismatch"})
                        else:
                            shutil.copytree(Path(tmp) / spec.name, job / "export" / spec.name)
                            export = {k: str(job / "export" / spec.name / Path(p).name) for k, p in staged.items()}
                            if any(
                                artifacts.sha256_file(Path(export[k])) != artifacts.sha256_file(Path(p))
                                for k, p in staged.items()
                            ):
                                errors.append({"code": "godot_hash_mismatch"})
            except (PipelineError, ValueError, OSError, subprocess.SubprocessError) as e:
                errors.append({"code": "godot_or_export_failed", "message": str(e)})
        # Last gate: inspect the actual child again after validation/Godot, before PASS.
        child_cells = {}
        try:
            source_cells = _sources(repair, doc)
            require(
                artifacts.sha256_file(repair / "workspace/doc.aseprite") == document_sha256,
                "document_hash_mismatch",
            )
            safe_tree(job / "raw")
            if {p.name for p in (job / "raw").iterdir()} != set(doc["source"]):
                errors.append({"code": "frame_set_mismatch"})
            child_pixels = {}
            for n, entry in mapping.items():
                name = source_name(entry)
                path = job / "raw" / name
                if not path.is_file():
                    continue
                chunks = png_chunks(path)
                if chunks:
                    errors.append({"code": "colour_chunks", "files": {name: chunks}})
                data = no_symlinks(path).read_bytes()
                if hashlib.sha256(data).hexdigest() != compared_hashes.get(name):
                    errors.append({"code": "compared_files_changed", "frame": n})
                before = source_cells[name]
                with Image.open(BytesIO(data)) as image:
                    after = np.asarray(image.convert("RGBA")).copy()
                child_cells[name] = after
                child_pixels[n] = Broker.pixel_hash(after.copy())
                if after.shape != before.shape:
                    errors.append({"code": "canvas_mismatch", "frame": n})
                    continue
                if any(pixel_problems(after, doc["palette"]).values()):
                    errors.append({"code": "invalid_pixels", "frame": n})
                changed = changed_coordinates(before, after)
                if any(n not in masks or not masks[n][y, x] for x, y in changed):
                    errors.append({"code": "unauthorized_pixels", "frame": n, "changed": changed})
            if child_pixels != expected_pixels:
                errors.append({"code": "checkpoint_mismatch" if accepted_hashes is not None else "session_required"})
            if export:
                sheet = load_rgba(Path(export["png"]))
                derived = slice_sheet(sheet, spec.cell, order, clip.frames, start_col=1)
                for direction, src in mirror.items():
                    for i in range(clip.frames):
                        require(
                            Broker.pixel_hash(derived[direction][i].copy())
                            == Broker.pixel_hash(child_cells[f"{src}_{i}.png"][:, ::-1].copy()),
                            "mirror_mismatch",
                        )
        except (ValueError, OSError, Refused) as e:
            errors.append({"code": getattr(e, "code", "child_verification_failed"), "message": str(e)})
        if errors:
            export = None
            if (job / "export").exists():
                shutil.rmtree("export", dir_fd=destination)
            rep["pass"] = False
            rep["failures"] = [*rep["failures"], *(e["code"] for e in errors)]
        rep["repair_errors"] = errors
        artifacts.write_json(job, "report.json", rep)
        outputs = {"report.json": artifacts.sha256_file(job / "report.json")}
        if clips:
            outputs |= artifacts.write_stage(job, "05_registered", clips)
        if clips and all(f.shape == (spec.cell[1], spec.cell[0], 4) for fs in clips.values() for f in fs):
            contact_sheet(clips, rep, cell=spec.cell, foot_y=spec.foot_y).save(job / "contact.png")
        else:
            before = {d: [source_cells[f"{d}_{i}.png"] for i in range(clip.frames)] for d in gen}
            contact_sheet(before, rep, cell=spec.cell, foot_y=spec.foot_y).save(job / "contact.png")
        outputs["contact.png"] = artifacts.sha256_file(job / "contact.png")
        before_frames = {d: [source_cells[f"{d}_{i}.png"] for i in range(clip.frames)] for d in gen}
        before_stills = {d: source_cells[f"still_{d}.png"] for d in order}
        cells_before, cells_after, frame_hashes = {}, {}, {}
        for n, entry in mapping.items():
            d, i = entry["direction"], entry["index"]
            before = before_stills[d] if entry["kind"] == "still" else before_frames[d][i]
            name = source_name(entry)
            after = child_cells.get(name)
            frame_hashes[name] = {
                "before": hashlib.sha256(before.tobytes()).hexdigest(),
                "after": hashlib.sha256(after.tobytes()).hexdigest() if after is not None else None,
                "changed": after is None or after.shape != before.shape or bool(changed_coordinates(before, after)),
            }
            if n in masks:
                cells_before[name] = [before]
                if after is not None and after.shape == before.shape:
                    cells_after[name] = [after]
                else:
                    cells_after[name + " (missing/invalid)"] = [np.zeros_like(before)]  # Review placeholder only.
        # Derive provenance for west animation cells through the same packer as the export.
        before_sheet = pack_sheet(before_frames, spec.cell, order, clip.frames, mirror=mirror)
        before_derived = slice_sheet(before_sheet, spec.cell, order, clip.frames, start_col=1)
        after_derived = {}
        try:
            after_sheet = pack_sheet(clips, spec.cell, order, clip.frames, mirror=mirror, required=order)
            after_derived = slice_sheet(after_sheet, spec.cell, order, clip.frames, start_col=1)
        except ValueError:
            pass  # An incomplete/invalid export is already a failed child; derived cells are absent.
        for d in mirror:
            for i, before in enumerate(before_derived[d]):
                after = after_derived[d][i] if d in after_derived else None
                frame_hashes[f"{d}_{i}.png"] = {
                    "before": hashlib.sha256(before.tobytes()).hexdigest(),
                    "after": hashlib.sha256(after.tobytes()).hexdigest() if after is not None else None,
                    "changed": after is None or bool(changed_coordinates(before, after)),
                }
        for name, cells in (("before", cells_before), ("after", cells_after)):
            if cells:
                contact_sheet(cells, rep, cell=spec.cell, foot_y=spec.foot_y, border=False).save(
                    job / f"repair-{name}.png"
                )
                outputs[f"repair-{name}.png"] = artifacts.sha256_file(job / f"repair-{name}.png")
        if export:
            outputs |= {Path(p).relative_to(job).as_posix(): artifacts.sha256_file(Path(p)) for p in export.values()}
        timings = {"batch_export_s": exported_at - started, "total_s": time.perf_counter() - started}
        manifest = artifacts.build_manifest(
            job=job,
            profile_path=profile.path,
            character=spec.name,
            animation=anim,
            inputs=inputs,
            outputs=outputs,
            params=params,
            timings=timings,
            upstream={"parent_job": doc["parent_job"], "hashes": doc["parent_hashes"]},
        )
        manifest["result"] = {"pass": not errors, "exported": export is not None, "exit": 1 if errors else 0}
        manifest["godot_check"] = gc
        manifest["repair"] = {
            "manifest": "repair-manifest.json",
            "repair_id": doc["repair_id"],
            "parent_job": doc["parent_job"],
        }
        artifacts.write_json(job, "manifest.json", manifest)
        changed = [name for name, v in frame_hashes.items() if v["changed"]]
        repair_manifest = {
            "schema": "sonoranlab.repair-manifest/1",
            "parent_job": doc["parent_job"],
            "parent_hashes": doc["parent_hashes"],
            "repair_id": doc["repair_id"],
            "source_hashes": doc["source"],
            "output_frame_hashes": inputs,
            "output_hashes": outputs,
            "child_manifest_sha256": artifacts.sha256_file(job / "manifest.json"),
            "document_sha256": artifacts.sha256_file(repair / "workspace/doc.aseprite"),
            "runtime": runtime,
            "prepared_runtime": doc["runtime"],
            "authorization": auth,
            "authorization_sha256": artifacts.sha256_file(repair / "authorization.json"),
            "operations": operations,
            "selected_frames": auth["targets"],
            "mirrored_derivations": [{"direction": d, "source": src} for d, src in mirror.items()],
            "frames": frame_hashes,
            "changed": changed,
            "unchanged": sorted(set(frame_hashes) - set(changed)),
            "target_diffs": {n: comparison.get("frames", {}).get(n) for n in masks},
            "comparison": comparison,
            "validator": validator,
            "validator_deltas": _measurements(parent_report, validator),
            "godot_check": gc,
            "timings": timings,
            "errors": errors,
            "human_review": "pending",
        }
        artifacts.write_json(job, "repair-manifest.json", repair_manifest)
        verify_destination(out, destination, forbidden=forbidden)
        return {
            "ok": not errors,
            "pass": not errors,
            "error": errors[0]["code"] if errors else None,
            "job": str(job),
            "repair": str(repair),
            "report": str(job / "report.json"),
            "contact": str(job / "contact.png"),
            "manifest": str(job / "manifest.json"),
            "repair_manifest": str(job / "repair-manifest.json"),
            "export": export,
            "human_review": "pending",
            "errors": errors,
        }
