"""`sonoranlab deliver`: an owner-gated, reversible delivery bundle for one composed class sheet (ADR-0011).

`build` (staging only) turns an owner-approved composed job (`review check` = production usable) into BUNDLE/:
bundle.json (`sonoranlab.delivery_bundle/1`), files/<game-relative path> (sheet, its .import, the patched
characters.json), characters.json.diff, checks/{before,after}.sha256, provenance.md, INSTALL.md, ROLLBACK.md.

`apply` / `verify` / `rollback` act on a **disposable snapshot** only: a clean git worktree of the game at the
bundle's target revision that is not (and does not resolve into) any protected game checkout (the profile's
`export.godot_project` and every repository profile's, the same roots `api/paths.py` protects). Apply backs up and
journals under BUNDLE/.applied/<target-id>/, installs each file atomically (tmp + rename) and rolls itself back on a
partial failure. Rollback restores the exact bytes. INSTALLED is never set here: installing into the real checkout is
the owner's step (INSTALL.md), recorded by the owner.

Exit codes: 0 ok, 1 verify/rollback found a problem, 2 refusal (nothing changed).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import numpy as np
import yaml
from PIL import Image

from sonoranlab import artifacts
from sonoranlab.compose import TOOL as COMPOSE_TOOL
from sonoranlab.profile import load_profile
from sonoranlab.review import _load as load_strict_json
from sonoranlab.review import review_errors

SCHEMA = "sonoranlab.delivery_bundle/1"
APPLY_SCHEMA = "sonoranlab.delivery_apply/1"
REQUEST_SCHEMA = "sonoranlab.class_request/1"
CONSUMER_SCHEMA = "sonoranlab.game_consumer_result/1"
GAME = "TwosAreUp"
STATES = ("ARTIFACT_APPROVED", "STAGED_AND_ENGINE_VERIFIED", "READY_TO_INSTALL", "INSTALLED")
# What the game's consumers need (staged PixelStandee.cs / PixelGallery.cs at dd4b2c1). A sheet without one of these is
# a visual delivery, not a playable class.
CONSUMER_CLIPS = {
    "idle": "PixelStandee.Present: standing pose",
    "run": "PixelStandee.Present: moving pose",
    "cast": 'PixelStandee.Present: selected while casting or shooting (_clips["cast"] has no fallback)',
    "death": "PixelGallery.Check (--pixel-check): the clip table requires a death clip",
}
_REV = re.compile(r"^[0-9a-f]{40}$")
_ID = re.compile(r"^[a-z0-9][a-z0-9_]*$")


class DeliveryError(Exception):
    """A refusal (exit 2): nothing in the target was changed."""


# ---------------------------------------------------------------- small helpers


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha(path: Path) -> str:
    artifacts.require_regular_file(path)
    return artifacts.sha256_file(path)


def _write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Same-directory tmp + fsync + rename: readers see the old or the new bytes, never a partial file."""
    tmp = path.parent / f".{path.name}.sonoranlab.tmp"
    try:
        with open(tmp, "xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _json_bytes(obj: object) -> bytes:
    return (json.dumps(obj, indent=1, sort_keys=True) + "\n").encode()


def _git(target: Path, *args: str) -> str:
    try:
        p = subprocess.run(["git", "-C", str(target), *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise DeliveryError(f"git {' '.join(args)}: {e}") from None
    if p.returncode:
        raise DeliveryError(f"{target}: git {' '.join(args)} failed: {p.stderr.strip()[:300]}")
    return p.stdout


def _status(target: Path) -> list[str]:
    """`git status` entries (tracked changes and untracked files; ignored files are not listed)."""
    out = _git(target, "status", "--porcelain=v1", "--untracked-files=all", "-z")
    return sorted(e for e in out.split("\0") if e)


def _safe_rel(path: str) -> PurePosixPath:
    p = PurePosixPath(path)
    if not path or "\\" in path or "\0" in path or p.is_absolute() or ".." in p.parts or str(p) != path:
        raise DeliveryError(f"bundle path {path!r} is not a plain relative path inside the game")
    return p


def protected_roots(profile: Path) -> tuple[Path, ...]:
    """The profile's game checkout plus every repository profile's (the api/paths.py policy, not widened)."""
    from sonoranlab.api.paths import _game_roots

    own = load_profile(profile).export.godot_project.expanduser().resolve()
    return tuple(dict.fromkeys((own, *_game_roots())))


def _refuse_protected(path: Path, roots: tuple[Path, ...], what: str) -> None:
    lexical, resolved = Path(os.path.abspath(path)), Path(os.path.realpath(path))
    for root in roots:
        if any(p == root or p.is_relative_to(root) for p in (lexical, resolved)):
            raise DeliveryError(f"{what} {path} is (or resolves into) the protected game checkout {root}")


def godot_uid(seed: str) -> str:
    """A deterministic `uid://` in Godot 4's text form (ResourceUID::id_to_text: base 34, a-y then 0-8; Godot 4.7.2
    rewrote a `z`, and none of the game's 119 shipped uids contains `z` or `9`)."""
    n = int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF
    chars = ""
    while True:
        c = n % 34
        chars = (chr(ord("a") + c) if c < 25 else chr(ord("0") + c - 25)) + chars
        n //= 34
        if not n:
            return "uid://" + chars


def import_file(template: str, *, res_png: str, uid: str) -> str:
    """The new sheet's `.png.import`, modelled on a sibling's: same importer and [params] (lossless, no mipmaps),
    the source/dest paths re-pointed (Godot names the imported file `<file>-<md5(res path)>.ctex`) and a fresh uid."""
    m = re.search(r'^source_file="(res://[^"]+\.png)"$', template, re.M)
    if not m or 'importer="texture"' not in template or "[params]" not in template:
        raise DeliveryError("--import-template is not a Godot texture .png.import (importer, source_file, [params])")
    for setting in ("compress/mode=0", "mipmaps/generate=false"):
        if not re.search(rf"^{re.escape(setting)}$", template, re.M):
            raise DeliveryError(f"--import-template lacks the pixel-art setting {setting}")
    old = m[1]
    old_name, new_name = PurePosixPath(old).name, PurePosixPath(res_png).name
    old_key = f"{old_name}-{hashlib.md5(old.encode()).hexdigest()}"
    if old_key not in template:
        raise DeliveryError(f"--import-template: imported path is not {old_key}.* (Godot's naming); not modelled")
    new_key = f"{new_name}-{hashlib.md5(res_png.encode()).hexdigest()}"
    text = template.replace(old_key, new_key).replace(f'"{old}"', f'"{res_png}"')
    text, n = re.subn(r'^uid="uid://[a-z0-9]+"$', f'uid="{uid}"', text, flags=re.M)
    if n > 1 or old_name in text:
        raise DeliveryError(f"--import-template: unexpected uid lines or leftover references to {old_name}")
    return text


def _manifest_style(doc: dict, raw: bytes) -> bool:
    """How the game serialises characters.json (indent 1, trailing newline); returns ensure_ascii. Refuses a base
    whose bytes do not round-trip, since rewriting it would touch unrelated entries."""
    for ascii_only in (True, False):
        if (json.dumps(doc, indent=1, ensure_ascii=ascii_only) + "\n").encode() == raw:
            return ascii_only
    raise DeliveryError("--base-manifest does not round-trip as indent-1 JSON; refusing to rewrite it")


def standing_pixels(sheet: np.ndarray, cell: int, south_row: int) -> int:
    """Lowest-to-highest opaque row span of the south still (sheet row `south_row`, column 0)."""
    still = sheet[south_row * cell : (south_row + 1) * cell, :cell, 3] > 0
    ys = np.nonzero(still.any(1))[0]
    if not len(ys):
        raise DeliveryError("the south still is empty")
    return int(ys.max() - ys.min() + 1)


def _request(path: Path, *, project: str, character: str) -> dict:
    """The fields delivery needs from a class request. ponytail: DYL-128's strict `classreq` loader replaces this
    once it lands; here unknown keys are tolerated and only these are checked."""
    try:
        artifacts.require_regular_file(path)
        doc = yaml.safe_load(path.read_text())
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise DeliveryError(f"--request {path}: unreadable ({e})") from None
    if not isinstance(doc, dict) or doc.get("schema", REQUEST_SCHEMA) != REQUEST_SCHEMA:
        raise DeliveryError(f"--request {path}: not a {REQUEST_SCHEMA} mapping")
    if doc.get("project") != project or doc.get("character") != character:
        raise DeliveryError(f"--request is for {doc.get('project')}/{doc.get('character')}, not {project}/{character}")
    h = doc.get("height_m")
    if type(h) not in (int, float) or not 0 < h < 20:
        raise DeliveryError("--request height_m must be a positive number of metres (never guessed)")
    clips = doc.get("clips", [])
    if not isinstance(clips, list) or not all(isinstance(c, str) for c in clips):
        raise DeliveryError("--request clips must be a list of clip names")
    return doc


# ---------------------------------------------------------------- build


def build(
    *,
    profile: Path,
    composed: Path,
    request: Path,
    target_rev: str,
    base_manifest: Path,
    import_template: Path,
    out: Path,
) -> dict:
    """Write BUNDLE (exclusive). Returns {"bundle", "state", "files", "known_missing_clips"}; DeliveryError refuses."""
    profile, composed, out = Path(profile).expanduser(), Path(composed).expanduser(), Path(out).expanduser()
    prof = load_profile(profile)
    roots = protected_roots(profile)
    if not _REV.match(target_rev):
        raise DeliveryError("--target-rev must be the full 40-hex commit of the game revision the bundle targets")
    if out.exists() or out.is_symlink():
        raise DeliveryError(f"--out {out} exists; bundles are created exclusively")
    _refuse_protected(out, roots, "--out")

    # ---- the owner-approved composed job
    if problems := review_errors(composed):
        raise DeliveryError(
            "the composed job is not production usable (owner 1x review required and bound to these files): "
            + "; ".join(f"{e['code']}: {e['message']}" for e in problems[:6])
        )
    manifest = load_strict_json(composed / "manifest.json")
    params = manifest.get("params") or {}
    if params.get("tool") != COMPOSE_TOOL:
        raise DeliveryError("--composed must be a `sonoranlab compose` job (one sheet with every clip)")
    if manifest["profile"]["sha256"] != _sha(profile):
        raise DeliveryError("--profile differs from the profile the composed job was validated with")
    cid = manifest["character"]
    if not _ID.match(cid):
        raise DeliveryError(f"character id {cid!r} is not a game id (lowercase, digits, underscore)")
    spec = prof.character(cid)
    req = _request(Path(request), project=prof.id, character=cid)
    review_sha = _sha(composed / "review.json")
    review = load_strict_json(composed / "review.json")
    report_sha = _sha(composed / "report.json")
    sheet_path = composed / "export" / cid / f"{cid}.png"
    sheet_bytes = sheet_path.read_bytes()
    sheet_sha = hashlib.sha256(sheet_bytes).hexdigest()
    if review["binds"]["export"].get(f"export/{cid}/{cid}.png") != sheet_sha:
        raise DeliveryError("the reviewed sheet hash differs from the sheet on disk")
    with Image.open(sheet_path) as im:
        sheet = np.asarray(im.convert("RGBA"))

    # ---- clip table vs the profile
    order = list(prof.directions.order)
    cell = spec.cell[0]
    clips = params.get("clips")
    if not isinstance(clips, list) or not clips:
        raise DeliveryError("composed job records no clip table")
    start, entry_clips = 1, []  # the game's key order: name, start, frames, fps, loop[, loop_from]
    for c in clips:
        a = spec.animations.get(c.get("name")) if isinstance(c, dict) else None
        if a is None:
            raise DeliveryError(f"clip {c} is not a clip of {cid} in the profile")
        want = {"name": c["name"], "start": start, "frames": a.frames, "fps": a.fps, "loop": a.loop}
        want |= {"loop_from": a.loop_from} if a.loop_from is not None else {}
        if c != want:
            raise DeliveryError(f"clip layout {c} does not match the profile ({want})")
        entry_clips.append(want)
        start += a.frames
    columns = start
    if sheet.shape[:2] != (len(order) * cell, columns * cell):
        raise DeliveryError(f"sheet {sheet.shape[1]}x{sheet.shape[0]} is not {columns}x{len(order)} cells of {cell}")
    names = [c["name"] for c in clips]
    for need in ("idle", "run"):
        if need not in names:
            raise DeliveryError(f"the sheet has no {need} clip; PixelStandee cannot display it (ADR-0011 pilot clips)")
    if missing := [c for c in req.get("clips", []) if c not in names]:
        raise DeliveryError(f"the class request requires clips {missing} that the sheet lacks")

    # ---- base manifest and the new entry
    base_manifest = Path(base_manifest).expanduser()
    try:
        artifacts.require_regular_file(base_manifest)
        base_raw = base_manifest.read_bytes()
        base = json.loads(base_raw)
    except (OSError, ValueError) as e:
        raise DeliveryError(f"--base-manifest: unreadable ({e})") from None
    ascii_only = _manifest_style(base, base_raw)
    if base.get("rows") != order or base.get("mirrored_animation_rows") != dict(prof.directions.mirror_animations):
        raise DeliveryError("--base-manifest rows / mirrored_animation_rows differ from the profile's directions")
    density = base.get("density_px_per_m")
    if prof.canvas.density_px_per_m is not None and density != prof.canvas.density_px_per_m:
        raise DeliveryError(f"--base-manifest density {density} differs from the profile's")
    if not isinstance(base.get("characters"), dict):
        raise DeliveryError("--base-manifest has no characters object")
    if cid in base["characters"]:
        raise DeliveryError(f"character {cid!r} already exists in the base manifest; delivery only adds new ids")
    standing = standing_pixels(sheet, cell, order.index("south"))
    if round(req["height_m"] * density) != standing:
        raise DeliveryError(
            f"height_m {req['height_m']} x {density} px/m = {req['height_m'] * density:.2f} px, but the south still "
            f"stands {standing} px (the game's entries satisfy standing_pixels = round(height_m x density))"
        )
    opaque = sheet[sheet[..., 3] > 0][:, :3]
    entry = {
        "sheet": f"{cid}.png",
        "cell": cell,
        "columns": columns,
        "standing_pixels": standing,
        "height_m": req["height_m"],
        "clips": entry_clips,
        "colours": len(np.unique(opaque, axis=0)),
    }
    patched = json.loads(base_raw)
    patched["characters"][cid] = entry
    patched_raw = (json.dumps(patched, indent=1, ensure_ascii=ascii_only) + "\n").encode()

    # ---- game-relative paths
    sprites = prof.export.sprites_dir
    if not sprites.startswith("res://"):
        raise DeliveryError(f"profile export.sprites_dir {sprites!r} is not a res:// path")
    rel_dir = sprites.removeprefix("res://").strip("/")
    res_png = f"res://{rel_dir}/{cid}.png"
    try:
        template = Path(import_template).expanduser()
        artifacts.require_regular_file(template)
        imp = import_file(template.read_text(), res_png=res_png, uid=godot_uid(f"{cid}:{sheet_sha}")).encode()
    except (OSError, UnicodeDecodeError) as e:
        raise DeliveryError(f"--import-template: unreadable ({e})") from None
    man_rel = f"{rel_dir}/{prof.export.manifest}"
    files = [
        {"path": f"{rel_dir}/{cid}.png", "action": "add", "sha256_before": None, "sha256_after": sheet_sha},
        {
            "path": f"{rel_dir}/{cid}.png.import",
            "action": "add",
            "sha256_before": None,
            "sha256_after": hashlib.sha256(imp).hexdigest(),
        },
        {
            "path": man_rel,
            "action": "modify",
            "sha256_before": hashlib.sha256(base_raw).hexdigest(),
            "sha256_after": hashlib.sha256(patched_raw).hexdigest(),
        },
    ]
    for f in files:
        _safe_rel(f["path"])
    payload = {files[0]["path"]: sheet_bytes, files[1]["path"]: imp, man_rel: patched_raw}
    missing_clips = [{"name": n, "needed_by": why} for n, why in CONSUMER_CLIPS.items() if n not in names]
    diff = "".join(
        difflib.unified_diff(
            base_raw.decode().splitlines(keepends=True),
            patched_raw.decode().splitlines(keepends=True),
            fromfile=f"a/{man_rel}",
            tofile=f"b/{man_rel}",
        )
    )
    source = {
        "composed_job": str(composed.resolve()),
        "sheet_sha256": sheet_sha,
        "review_record_sha256": review_sha,
        "validator_report_sha256": report_sha,
        "request": str(Path(request).resolve()),
        "request_sha256": _sha(Path(request)),
        "profile": {"path": str(profile.resolve()), "sha256": _sha(profile)},
        "base_manifest": str(base_manifest.resolve()),
        "import_template": str(template.resolve()),
        "import_template_sha256": _sha(template),
    }
    target = {"game": GAME, "revision": target_rev, "base_manifest_sha256": files[2]["sha256_before"]}
    docs = _docs(cid=cid, files=files, missing=missing_clips, target=target)
    docs["provenance.md"] = _provenance_md(cid, composed, manifest, review, source, files)

    # ---- write (exclusive dir, bundle.json last)
    out.mkdir(parents=True)
    for rel, data in payload.items():
        p = out / "files" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (out / "checks").mkdir()
    (out / "characters.json.diff").write_text(diff)
    for name, text in docs.items():
        (out / name).write_text(text)
    bundle_files = {
        p.relative_to(out).as_posix(): artifacts.sha256_file(p) for p in sorted(out.rglob("*")) if p.is_file()
    }
    doc = {
        "schema": SCHEMA,
        "character": cid,
        "display_name": req.get("display_name"),
        "state": "BUILT",
        "source": source,
        "target": target,
        "files": files,
        "manifest_entry": entry,
        "known_missing_clips": missing_clips,
        "playable_class": not missing_clips,
        "states": {
            "ARTIFACT_APPROVED": {
                "value": True,
                "evidence": {
                    "review_record": str((composed / "review.json").resolve()),
                    "sha256": review_sha,
                    "verdict": review.get("verdict"),
                    "reviewer": review.get("reviewer"),
                },
            },
            "STAGED_AND_ENGINE_VERIFIED": {"value": False, "evidence": None},
            "READY_TO_INSTALL": {"value": False, "evidence": None},
            "INSTALLED": {"value": False, "evidence": None, "set_by": "the owner's install record only (INSTALL.md)"},
        },
        "bundle_files": bundle_files,
        "created_at": _now(),
    }
    (out / "bundle.json").write_bytes(_json_bytes(doc))
    return {"bundle": str(out), "state": "BUILT", "files": files, "known_missing_clips": missing_clips}


def _docs(*, cid: str, files: list[dict], missing: list[dict], target: dict) -> dict:
    """INSTALL.md / ROLLBACK.md (exact owner-run commands) and the sha256sum check files they use."""
    adds = [f["path"] for f in files if f["action"] == "add"]
    mods = [f["path"] for f in files if f["action"] == "modify"]
    rev = target["revision"]
    expect = ", ".join([*(f"?? {p}" for p in adds), *(f" M {p}" for p in mods)])
    gap = []
    if missing:
        clips = ", ".join(f"`{m['name']}` ({m['needed_by']})" for m in missing)
        gap = [f"**Visual delivery with known missing clips, not a playable class:** {clips}.", ""]
    install = [
        f"# Install `{cid}` into Twos Are Up (owner only)",
        "",
        *gap,
        "Nothing in SonoranLab runs these commands, and `sonoranlab deliver apply` refuses the real checkout. They",
        f"copy {len(files)} files into a clean checkout at exactly revision `{rev}`; a failing check means stop.",
        "",
        "```bash",
        "set -euo pipefail",
        'B="$(pwd)"                      # run from this bundle directory',
        "GAME=~/Projects/TwosAreUp       # the real checkout",
        '(cd "$B/files" && sha256sum --strict -c "$B/checks/after.sha256")   # bundle files are the built ones',
        'cd "$GAME"',
        f'test "$(git rev-parse HEAD)" = "{rev}"',
        'test -z "$(git status --porcelain --untracked-files=all)"',
        'sha256sum --strict -c "$B/checks/before.sha256"   # the base manifest the bundle patched',
        *(f'test ! -e "{p}"' for p in adds),
        *(f'install -m 0644 "$B/files/{f["path"]}" "{f["path"]}"' for f in files),
        'sha256sum --strict -c "$B/checks/after.sha256"',
        f"git status --short              # expect exactly: {expect}",
        "```",
        "",
        "Then open the project in the Godot editor once (it imports the sheet; if it rewrites",
        f"`{cid}.png.import`, keep its version), run the game's own checks, review `git diff` and commit. Record the",
        "installation yourself in `INSTALLED.json` next to this file (game commit, date, your name). No SonoranLab",
        "command sets INSTALLED.",
        "",
    ]
    rollback = [
        f"# Roll back `{cid}` (owner only)",
        "",
        "Before committing the install:",
        "",
        "```bash",
        "set -euo pipefail",
        'B="$(pwd)"; GAME=~/Projects/TwosAreUp',
        'cd "$GAME"',
        *(f'git checkout -- "{p}"' for p in mods),
        *(f'rm -f -- "{p}"' for p in adds),
        'sha256sum --strict -c "$B/checks/before.sha256"',
        'test -z "$(git status --porcelain --untracked-files=all)"',
        "```",
        "",
        "After committing it: `git revert <install commit>`. A disposable snapshot is rolled back with",
        '`sonoranlab deliver rollback "$B" --target SNAPSHOT`, which restores the exact bytes from its backup.',
        "",
    ]
    return {
        "INSTALL.md": "\n".join(install),
        "ROLLBACK.md": "\n".join(rollback),
        "checks/before.sha256": "".join(f"{f['sha256_before']}  {f['path']}\n" for f in files if f["sha256_before"]),
        "checks/after.sha256": "".join(f"{f['sha256_after']}  {f['path']}\n" for f in files),
    }


def _provenance_md(cid: str, composed: Path, manifest: dict, review: dict, source: dict, files: list[dict]) -> str:
    report = load_strict_json(composed / "report.json")
    lines = [f"# Provenance: `{cid}`", "", f"- composed job: `{source['composed_job']}`"]
    for name, s in (report.get("provenance") or {}).get("sources", {}).items():
        lines.append(f"- clip `{name}`: job `{s['job']}` (sheet {s['sheet_sha256']}, report {s['report_sha256']})")
    lines += ["", "## Model inventory (from each clip job's manifest upstream)", ""]
    for name, up in (manifest.get("upstream") or {}).items():
        for fname, rec in (up or {}).items():
            fields = ", ".join(f"{k}={v}" for k, v in sorted(rec.items()) if k != "sha256")
            lines.append(f"- `{name}` {fname} ({rec.get('sha256')}): {fields or 'no model fields recorded'}")
    lines += [
        "",
        "## Owner review",
        "",
        f"- verdict `{review.get('verdict')}` by {review.get('reviewer')}, {review.get('started_at')} to "
        f"{review.get('ended_at')}",
        f"- review.json sha256 {source['review_record_sha256']}; validator report {source['validator_report_sha256']}",
        "",
        "## Hashes",
        "",
        f"- request {source['request_sha256']} (`{source['request']}`)",
        f"- profile {source['profile']['sha256']} (`{source['profile']['path']}`)",
        f"- import template {source['import_template_sha256']} (`{source['import_template']}`)",
        *(f"- {f['action']} `{f['path']}`: {f['sha256_before']} -> {f['sha256_after']}" for f in files),
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- bundle integrity and the target


def load_bundle(bundle: Path) -> dict:
    """bundle.json plus every check that its files, the owner review and the sheet are still the built ones."""
    bundle = Path(bundle)
    try:
        doc = load_strict_json(bundle / "bundle.json")
    except ValueError as e:
        raise DeliveryError(str(e)) from None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise DeliveryError(f"{bundle}: not a {SCHEMA} bundle")
    listed = doc.get("bundle_files") or {}
    on_disk = {
        p.relative_to(bundle).as_posix()
        for p in bundle.rglob("*")
        if (p.is_file() or p.is_symlink()) and p.name != "bundle.json" and ".applied" not in p.relative_to(bundle).parts
    }
    if on_disk != set(listed):
        raise DeliveryError(f"bundle files differ from bundle.json: {sorted(on_disk ^ set(listed))}")
    for rel, sha in listed.items():
        if _sha(bundle / _safe_rel(rel)) != sha:
            raise DeliveryError(f"bundle file {rel} was modified after build")
    for f in doc["files"]:
        if _sha(bundle / "files" / _safe_rel(f["path"])) != f["sha256_after"]:
            raise DeliveryError(f"bundle file files/{f['path']} differs from bundle.json")
    src = doc["source"]
    composed = Path(src["composed_job"])
    if problems := review_errors(composed):
        raise DeliveryError("the owner review no longer binds the composed job: " + problems[0]["message"])
    if _sha(composed / "review.json") != src["review_record_sha256"]:
        raise DeliveryError("the composed job's review.json is not the one the bundle was built from")
    sheet = composed / "export" / doc["character"] / f"{doc['character']}.png"
    if _sha(sheet) != src["sheet_sha256"] or doc["files"][0]["sha256_after"] != src["sheet_sha256"]:
        raise DeliveryError("the reviewed sheet differs from the bundle's sheet")
    return doc


def _target(doc: dict, target: Path) -> tuple[Path, str]:
    """Refuse a protected checkout and anything but a git worktree root at the bundle's revision."""
    roots = protected_roots(Path(doc["source"]["profile"]["path"]))
    _refuse_protected(target, roots, "--target")
    real = Path(os.path.realpath(target))
    if not real.is_dir():
        raise DeliveryError(f"--target {target} is not a directory")
    top = Path(os.path.realpath(_git(real, "rev-parse", "--show-toplevel").strip()))
    if top != real:
        raise DeliveryError(f"--target {target} is not the root of a git worktree (root: {top})")
    head = _git(real, "rev-parse", "HEAD").strip()
    if head != doc["target"]["revision"]:
        raise DeliveryError(f"--target HEAD {head} is not the bundle's target revision {doc['target']['revision']}")
    return real, hashlib.sha256(str(real).encode()).hexdigest()[:16]


def _dest(real: Path, rel: str) -> Path:
    """The file's path in the target; refuses escapes and symlinked or missing parent directories."""
    p = real
    for part in _safe_rel(rel).parts[:-1]:
        p = p / part
        if p.is_symlink() or not p.is_dir():
            raise DeliveryError(f"{rel}: parent {p} is a symlink or not a directory")
    dest = p / PurePosixPath(rel).name
    if dest.is_symlink():
        raise DeliveryError(f"{rel}: is a symlink in the target")
    if not Path(os.path.realpath(dest)).is_relative_to(real):
        raise DeliveryError(f"{rel}: escapes the target")
    return dest


def _expected_status(files: list[dict]) -> list[str]:
    return sorted(("?? " if f["action"] == "add" else " M ") + f["path"] for f in files)


def _install_file(data: bytes, dest: Path, mode: int) -> None:
    """One atomic file install (a seam for the partial-failure test)."""
    _write_atomic(dest, data, mode)


def _next_name(base: Path, tag: str) -> Path:
    n = 1
    while (p := base.with_name(f"{base.name}.{tag}-{n}")).exists():
        n += 1
    return p


# ---------------------------------------------------------------- apply / rollback / verify


def apply(bundle: Path, target: Path) -> dict:
    """Install the bundle into a disposable snapshot. Refusals raise DeliveryError before anything changes."""
    bundle = Path(bundle)
    doc = load_bundle(bundle)
    real, tid = _target(doc, Path(target))
    state = bundle / ".applied" / tid
    if state.exists():
        raise DeliveryError(f"already applied to {real} (journal {state / 'journal.json'}); roll back first")
    if dirty := _status(real):
        raise DeliveryError(f"--target is dirty ({len(dirty)} entries, e.g. {dirty[0]!r}); apply needs a clean tree")
    plan = []
    for f in doc["files"]:
        dest = _dest(real, f["path"])
        if f["action"] == "add":
            if dest.exists():
                raise DeliveryError(f"{f['path']}: already exists in the target (stale base)")
            mode = 0o644
        elif f["action"] == "modify":
            if not dest.is_file() or _sha(dest) != f["sha256_before"]:
                raise DeliveryError(f"{f['path']}: sha256 differs from the bundle's base (stale base); not applied")
            mode = dest.stat().st_mode & 0o7777
        else:
            raise DeliveryError(f"{f['path']}: unknown action {f['action']!r}")
        plan.append((f, dest, mode))
    state.parent.mkdir(exist_ok=True)
    try:
        state.mkdir()
    except FileExistsError:
        raise DeliveryError(f"already applied to {real} (journal {state / 'journal.json'}); roll back first") from None
    journal = {
        "schema": APPLY_SCHEMA,
        "status": "applying",
        "target": str(real),
        "head": doc["target"]["revision"],
        "files": doc["files"],
        "backups": {},
        "started_at": _now(),
    }
    done = []
    try:
        for f, dest, mode in plan:  # back up before touching anything
            if f["action"] == "modify":
                b = state / "backup" / f["path"]
                b.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(dest, b)
                if _sha(b) != f["sha256_before"]:
                    raise OSError(f"{f['path']}: backup does not match the base hash")
                journal["backups"][f["path"]] = {"sha256": f["sha256_before"], "mode": mode}
        _write_atomic(state / "journal.json", _json_bytes(journal))
        for f, dest, mode in plan:
            _install_file((bundle / "files" / f["path"]).read_bytes(), dest, mode)
            done.append((f, dest, mode))
            if _sha(dest) != f["sha256_after"]:
                raise OSError(f"{f['path']}: installed bytes differ from the bundle")
        if (got := _status(real)) != _expected_status(doc["files"]):
            raise OSError(f"target status after apply is {got}, expected {_expected_status(doc['files'])}")
    except (OSError, ValueError) as e:
        problems = _restore(state, done)
        journal |= {"status": "failed_rolled_back", "error": str(e), "restore_problems": problems, "ended_at": _now()}
        _write_atomic(state / "journal.json", _json_bytes(journal))
        state.rename(_next_name(state, "failed"))
        raise DeliveryError(
            f"apply failed and was rolled back ({len(done)} file(s) restored): {e}"
            + (f"; RESTORE PROBLEMS: {problems}" if problems else "")
        ) from None
    journal |= {"status": "applied", "ended_at": _now()}
    _write_atomic(state / "journal.json", _json_bytes(journal))
    return {"applied": str(real), "journal": str(state / "journal.json"), "files": [f["path"] for f in doc["files"]]}


def _restore(state: Path, done: list[tuple[dict, Path, int]]) -> list[str]:
    """Undo installed files (reverse order) to their exact base bytes; returns what could not be restored."""
    problems = []
    for f, dest, mode in reversed(done):
        try:
            if f["action"] == "add":
                if dest.exists() or dest.is_symlink():
                    dest.unlink()
                if dest.exists():
                    problems.append(f"{f['path']}: still exists")
            else:
                _write_atomic(dest, (state / "backup" / f["path"]).read_bytes(), mode)
                if _sha(dest) != f["sha256_before"]:
                    problems.append(f"{f['path']}: restored bytes differ from sha256_before")
        except (OSError, ValueError) as e:
            problems.append(f"{f['path']}: {e}")
    return problems


def _journal(bundle: Path, tid: str) -> tuple[Path, dict]:
    state = Path(bundle) / ".applied" / tid
    try:
        journal = load_strict_json(state / "journal.json")
    except ValueError:
        raise DeliveryError(f"not applied to this target (no journal under {state})") from None
    if journal.get("status") != "applied":
        raise DeliveryError(f"journal status is {journal.get('status')!r}, not applied")
    return state, journal


def rollback(bundle: Path, target: Path) -> dict:
    """Restore the exact pre-apply bytes from the journal's backups and check the target is clean again."""
    bundle = Path(bundle)
    try:
        doc = load_strict_json(bundle / "bundle.json")
    except ValueError as e:
        raise DeliveryError(str(e)) from None
    real, tid = _target(doc, Path(target))
    state, journal = _journal(bundle, tid)
    if journal["target"] != str(real):
        raise DeliveryError("journal target differs from --target")
    plan, changed = [], []
    for f in journal["files"]:
        dest = _dest(real, f["path"])
        if f["action"] == "modify":
            b = state / "backup" / f["path"]
            if _sha(b) != journal["backups"][f["path"]]["sha256"]:
                raise DeliveryError(f"{f['path']}: backup was modified; restore by hand")
        if dest.is_file() and _sha(dest) != f["sha256_after"]:
            changed.append(f["path"])
        plan.append((f, dest, journal["backups"].get(f["path"], {}).get("mode", 0o644)))
    problems = _restore(state, plan)
    status = _status(real)
    ok = not problems and not status
    record = {
        "status": "rolled_back" if ok else "rollback_incomplete",
        "changed_since_apply": changed,
        "restore_problems": problems,
        "target_status_after": status,
        "rolled_back_at": _now(),
    }
    journal |= record
    _write_atomic(state / "journal.json", _json_bytes(journal))
    if ok:
        state.rename(_next_name(state, "rolledback"))
    return {"ok": ok, "target": str(real), **record}


def verify(bundle: Path, target: Path, consumer_result: Path | None = None) -> dict:
    """Check the applied snapshot (and, with `consumer_result`, the game-consumer run on it) and record the states.
    READY_TO_INSTALL needs both; INSTALLED is never touched."""
    bundle = Path(bundle)
    doc = load_bundle(bundle)
    real, tid = _target(doc, Path(target))
    state, _ = _journal(bundle, tid)
    problems, engine = [], []
    for f in doc["files"]:
        dest = _dest(real, f["path"])
        got = _sha(dest) if dest.is_file() else None
        if got != f["sha256_after"]:
            problems.append(f"{f['path']}: sha256 {got} != bundle {f['sha256_after']}")
    if (status := _status(real)) != _expected_status(doc["files"]):
        problems.append(f"target status {status} != expected {_expected_status(doc['files'])}")
    consumer = None
    if consumer_result is None:
        engine.append("no game-consumer result given (tools/game_consumer/run.sh): engine not verified")
    else:
        consumer = {"path": str(Path(consumer_result).resolve())}
        try:
            res = load_strict_json(Path(consumer_result))
            consumer["sha256"] = _sha(Path(consumer_result))
        except ValueError as e:
            res = {}
            engine.append(f"consumer result: {e}")
        want = {f["path"]: f["sha256_after"] for f in doc["files"]}
        for key, value in (
            ("schema", CONSUMER_SCHEMA),
            ("pass", True),
            ("character", doc["character"]),
            ("target_head", doc["target"]["revision"]),
            ("files_sha256", want),
        ):
            if not isinstance(res, dict) or res.get(key) != value:
                got = res.get(key) if isinstance(res, dict) else None
                engine.append(f"consumer result {key} = {got!r}, expected {value!r}")
    files_ok = not problems
    engine_ok = files_ok and not engine
    problems += [] if consumer_result is None else engine
    n = 1
    while (rec_path := state / f"verify-{n}.json").exists():
        n += 1
    record = {
        "schema": "sonoranlab.delivery_verify/1",
        "target": str(real),
        "head": doc["target"]["revision"],
        "files_ok": files_ok,
        "consumer": consumer,
        "engine_verified": engine_ok,
        "problems": problems,
        "engine_pending": engine if consumer_result is None else [],
        "at": _now(),
    }
    with open(rec_path, "xb") as fh:
        fh.write(_json_bytes(record))
    evidence = {"verify_record": str(rec_path), "sha256": _sha(rec_path), "consumer_result": consumer}
    doc["states"]["STAGED_AND_ENGINE_VERIFIED"] = {"value": engine_ok, "evidence": evidence}
    doc["states"]["READY_TO_INSTALL"] = {"value": engine_ok and doc["states"]["ARTIFACT_APPROVED"]["value"]}
    doc["states"]["READY_TO_INSTALL"]["evidence"] = evidence if engine_ok else None
    doc["state"] = "READY_TO_INSTALL" if doc["states"]["READY_TO_INSTALL"]["value"] else "BUILT"
    _write_atomic(bundle / "bundle.json", _json_bytes(doc))
    return {
        "ok": not problems,
        "state": doc["state"],
        "files_ok": files_ok,
        "engine_verified": engine_ok,
        "problems": problems,
        "record": str(rec_path),
    }
