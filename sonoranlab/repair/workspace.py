"""Immutable repair inputs, deterministic document plans, and append-only batch exports.

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

import json
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sonoranlab.artifacts import _git_commit, ingest_raw, sha256_file, write_json
from sonoranlab.profile import Profile, load_palette, load_profile
from sonoranlab.repair import Refused
from sonoranlab.repair.compare import (
    expected_mapping,
    expected_metadata,
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
from sonoranlab.repair.runtime import AsepriteConverter, no_symlinks, seed_home

SCHEMA = "sonoranlab.repair/1"


class FakeConverter:
    """Test-only document: JSON metadata + independent PNG copies; never pretends to be an Aseprite file."""

    def record(self) -> dict:
        return {"converter": "fake", "version": 1}

    def build(self, workspace: Path) -> None:
        plan = read_json(workspace / "plan.json")
        frames = workspace / "fake-frames"
        frames.mkdir()
        for n, entry in enumerate(plan["frames"], 1):
            shutil.copyfile(workspace / "input" / Path(entry["png"]).name, frames / f"{n}.png")
        write_json(workspace, "doc.aseprite", {"fake_converter": 1, "meta": plan["metadata"]})

    def export(self, workspace: Path, output: Path) -> None:
        doc = read_json(workspace / "doc.aseprite")
        if doc.get("fake_converter") != 1:
            raise Refused("FakeConverter only reads its test document format")
        for path in (workspace / "fake-frames").glob("*.png"):
            shutil.copyfile(no_symlinks(path), output / path.name)
        write_json(output, "meta.json", doc["meta"])


def _check_tree(root: Path) -> None:
    no_symlinks(root)
    for p in root.rglob("*"):
        no_symlinks(p)


def _parent_hashes(parent: Path, sources: dict[str, Path], profile: Profile, character: str, anim: str) -> dict:
    _check_tree(parent)
    for p in sources.values():
        if not p.is_file():
            raise FileNotFoundError(f"{p}: not a regular source file")
    report, manifest = read_json(parent / "report.json"), read_json(parent / "manifest.json")
    for key in ("result", "profile", "inputs", "outputs"):
        if not isinstance(manifest.get(key, {}), dict):
            raise ValueError(f"manifest.json {key}: expected JSON object")
    if report.get("pass") is not True or manifest.get("result", {}).get("pass") is not True:
        raise Refused("repair requires a passing parent job")
    if manifest["result"].get("exported") is not True:
        raise Refused("repair requires an exported parent job")
    if manifest.get("character") != character or manifest.get("animation") != anim:
        raise Refused("parent character/animation does not match the repair")
    if manifest.get("profile", {}).get("sha256") != sha256_file(profile.path):
        raise Refused("parent profile sha256 differs")
    paths = [parent / "report.json", parent / "manifest.json"]
    if (parent / "review.json").exists():
        paths.append(parent / "review.json")
    exports = sorted(p for p in (parent / "export").rglob("*") if p.is_file())
    if not exports:
        raise Refused("parent has no export files")
    paths += exports
    hashes = {p.relative_to(parent).as_posix(): sha256_file(p) for p in paths}
    outputs = manifest.get("outputs", {})
    if {p.relative_to(parent).as_posix() for p in exports} != {k for k in outputs if k.startswith("export/")}:
        raise Refused("parent export files differ from manifest")
    for p in [parent / "report.json", *exports, *sources.values()]:
        rel = p.relative_to(parent).as_posix()
        want = manifest.get("inputs", {}).get(p.name) if rel.startswith("raw/") else outputs.get(rel)
        if sha256_file(p) != want:
            raise Refused(f"{rel}: parent sha256 differs from manifest")
    return hashes


def prepare(
    parent_job: Path,
    profile: Profile | Path,
    character: str,
    anim: str,
    out: Path,
    *,
    converter: AsepriteConverter | FakeConverter | None = None,
) -> dict:
    """Reserve a new repair, copy byte-exact immutable sources, and build only from disposable workspace copies."""
    parent = no_symlinks(parent_job)
    profile = profile if isinstance(profile, Profile) else load_profile(no_symlinks(profile))
    no_symlinks(profile.path)
    no_symlinks(profile.palette.source)
    palette_checkout = git_root(profile.palette.source)
    root = repair_root()
    forbidden = (parent, profile.export.godot_project, *((palette_checkout,) if palette_checkout else ()))
    out = check_destination(out, root=root, forbidden=forbidden)
    spec = profile.character(character)
    if anim not in spec.animations:
        raise ValueError(f"unknown animation: {anim}")
    animation = spec.animations[anim]
    duration = 1000 / animation.fps
    if not duration.is_integer():
        raise Refused(f"1000/fps must be an integer number of milliseconds (fps={animation.fps})")
    palette = ["#" + bytes(row).hex() for row in load_palette(profile)]
    doc = {
        "schema": SCHEMA,
        "repair_id": uuid.uuid4().hex,
        "parent_job": str(parent),
        "profile": {"path": str(profile.path.resolve()), "sha256": sha256_file(profile.path)},
        "character": character,
        "anim": anim,
        "cell": list(spec.cell),
        "fps": animation.fps,
        "frames": animation.frames,
        "loop": animation.loop,
        "duration_ms": int(duration),
        "directions": {
            "generate": list(profile.directions.generate),
            "order": list(profile.directions.order),
            "mirror": dict(profile.directions.mirror_animations),
        },
        "palette": palette,
        "git": _git_commit(),
        "created_at": datetime.now(UTC).isoformat(),
        "state": "prepared",
    }
    mapping = expected_mapping(doc)
    sources = {
        source_name(e): parent / ("raw" if e["kind"] == "still" else "processed/05_registered") / source_name(e)
        for e in mapping.values()
    }
    doc["parent_hashes"] = _parent_hashes(parent, sources, profile, character, anim)
    frame_dir = parent / "processed/05_registered"
    if {p.name for p in frame_dir.glob("*.png")} != {source_name(e) for e in mapping.values() if e["kind"] == "anim"}:
        raise Refused("registered frames differ from the generated-direction mapping")
    for p in sources.values():
        frame = load_rgba(p)
        if frame.shape[:2] != (spec.cell[1], spec.cell[0]) or any(pixel_problems(frame, palette).values()):
            raise Refused(f"{p}: source violates canvas/palette/binary-alpha contract")
    converter = converter or AsepriteConverter()
    doc["runtime"] = converter.record()
    doc["metadata"] = expected_metadata(doc)
    out, destination = create_destination(out, root=root, kind="repair", id=doc["repair_id"], forbidden=forbidden)
    try:
        (out / "raw").mkdir()
        doc["source"] = ingest_raw(out, sources)
        (out / "raw").rename(out / "source")
        doc["source_png_chunks"] = {name: png_chunks(out / "source" / name) for name in sources}
        workspace = out / "workspace"
        workspace.mkdir()
        (workspace / "input").mkdir()
        for name in sources:
            shutil.copyfile(out / "source" / name, workspace / "input" / name)
        seed_home(out / "session/home")
        write_json(out, "mapping.json", mapping)
        write_json(
            workspace,
            "plan.json",
            {
                "width": spec.cell[0],
                "height": spec.cell[1],
                "palette": palette,
                "frames": [
                    {"png": f"/work/input/{source_name(e)}", "duration_ms": e["duration_ms"]} for e in mapping.values()
                ],
                "tags": doc["metadata"]["tags"],
                "metadata": doc["metadata"],
            },
        )
        converter.build(workspace)
        doc["document_sha256"] = sha256_file(workspace / "doc.aseprite")
        write_json(out, "repair.json", doc)
        verify_destination(out, destination, forbidden=forbidden)
        return {
            "ok": True,
            "repair": str(out),
            "repair_id": doc["repair_id"],
            "state": "prepared",
            "frames": len(mapping),
        }
    finally:
        os.close(destination)


def inspect(repair: Path) -> dict:
    repair = no_symlinks(repair)
    doc = read_json(repair / "repair.json")
    if doc.get("schema") != SCHEMA:
        raise Refused("unknown repair schema")
    state = repair / "state.json"
    return {
        "ok": True,
        "repair": str(repair),
        "record": doc,
        "state": read_json(state)["state"] if state.exists() else doc["state"],
    }


def export(repair: Path, *, converter: AsepriteConverter | FakeConverter | None = None) -> dict:
    """Append a new export snapshot; retain failed attempts, which hold diff until another export succeeds."""
    repair = admit_repair(repair)
    repair = no_symlinks(repair)
    info = inspect(repair)
    if info["state"] != "prepared":
        raise Refused(f"repair is {info['state']}")
    _check_tree(repair)
    doc = info["record"]
    mapping = expected_mapping(doc)
    if read_json(repair / "mapping.json") != mapping:
        raise Refused("repair mapping differs")
    converter = converter or AsepriteConverter()
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
    _check_tree(output)
    meta = read_json(output / "meta.json")
    meta["png_chunks"] = {p.name: png_chunks(p) for p in sorted(output.glob("*.png"))}
    # This augments the new snapshot's converter metadata once; earlier snapshots are never opened for writing.
    (output / "meta.json").write_text(json.dumps(meta, sort_keys=True, indent=1) + "\n")
    write_json(output, "mapping.json", mapping)
    mapped = output / "mapped"
    mapped.mkdir()
    for n, entry in mapping.items():
        if (output / f"{n}.png").is_file():
            shutil.copyfile(output / f"{n}.png", mapped / source_name(entry))
    write_json(
        output,
        "export.json",
        {
            "repair_id": doc["repair_id"],
            "created_at": datetime.now(UTC).isoformat(),
            "doc_sha256": sha256_file(workspace / "doc.aseprite"),
            "files": {
                p.relative_to(output).as_posix(): sha256_file(p) for p in sorted(output.rglob("*")) if p.is_file()
            },
        },
    )
    return {"ok": True, "repair": str(repair), "export": str(output), "frames": len(list(output.glob("*.png")))}


def abort(repair: Path) -> dict:
    repair = admit_repair(repair)
    repair = no_symlinks(repair)
    info = inspect(repair)
    if info["state"] != "aborted":
        write_json(repair, "state.json", {"state": "aborted", "created_at": datetime.now(UTC).isoformat()})
    return {"ok": True, "repair": str(repair), "state": "aborted"}
