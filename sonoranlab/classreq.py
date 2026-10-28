"""Class-request intake (ADR-0011): a strict `sonoranlab.class_request/1` parser and the admission check of a request
plus its eight independent stills against the project profile.

The check is a pure read. It reports what blocks a campaign and never fixes, mirrors, synthesises or approves
anything. Still pixels go through the validator's own cell checks (`validate.validate`, no thresholds here); this
module adds only the still-set rules the validator does not know: the full direction set, the foot row, a baked
white sticker border, west stills that are exact mirrors of their east sources, and the cell size of a character
without a profile row. Every free-text field is untrusted data: bounded, control characters refused, never
interpreted.

Status precedence: invalid > needs_reference_preparation > needs_profile_row > ready.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from PIL import Image

from sonoranlab.profile import NAME_RE, Profile

SCHEMA = "sonoranlab.class_request/1"
RESULT_SCHEMA = "sonoranlab.class_request_check/1"
STATUSES = ("invalid", "needs_reference_preparation", "needs_profile_row", "ready")  # precedence order
SUPPORTED_TRACKS = ("E",)  # Phase 5 pilot: Track E clips only (run, idle)
MAX_TEXT = 4096  # characters per free-text field
MAX_ITEMS = 64  # entries per list
MAX_FILE = 256 * 1024  # bytes of a request file
MAX_STILL_EDGE = 1024  # px; a still is a final-resolution cell (<= 288): refuse anything far larger
CELL_MIN, CELL_MAX = 120, 288  # the game's per-character cell range (profile canvas comment, ADR-0007)
HEIGHT_RANGE = (0.2, 10.0)  # metres; sanity bound for a game standing height
WHITE = (255, 255, 255)
_BAD_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")  # newline and tab allowed
TEXT_KEYS = ("display_name", "status", "visual_role", "silhouette", "palette_notes", "source_design", "gameplay")
REQUIRED = ("schema", "project", "character", "display_name", "status", "height_m", "visual_role", "silhouette")
REQUIRED += ("invariants", "clips")
KEYS = (*REQUIRED, "palette_notes", "source_design", "gameplay", "references", "owner_decisions")
REF_KEYS = ("stills_dir", "provenance", "approved_by")


# ---------------------------------------------------------------- request


def _shown(k: Any) -> str:
    """A key name safe to echo back: untrusted text is truncated and escaped, never printed raw."""
    return repr(k)[:64]


def _text(v: Any, field: str, errs: list[str], *, limit: int = MAX_TEXT) -> str | None:
    if not isinstance(v, str):
        errs.append(f"{field}: expected a string, got {type(v).__name__}")
        return None
    if len(v) > limit:
        errs.append(f"{field}: {len(v)} characters, limit {limit}")
        return None
    if m := _BAD_CHARS.search(v):
        errs.append(f"{field}: control character U+{ord(m[0]):04X} at {m.start()} (only newline and tab are allowed)")
        return None
    return v


def _texts(v: Any, field: str, errs: list[str], *, nonempty: bool = False) -> list[str] | None:
    if not isinstance(v, list) or len(v) > MAX_ITEMS or (nonempty and not v):
        errs.append(f"{field}: expected a {'non-empty ' if nonempty else ''}list of at most {MAX_ITEMS} strings")
        return None
    out = [_text(x, f"{field}[{i}]", errs) for i, x in enumerate(v)]
    return None if None in out else out


def request_sha256(raw: Any) -> str:
    """Canonical-JSON hash of the parsed request: the same for a YAML file, a JSON file and an MCP object."""
    text = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def parse_request(raw: Any) -> tuple[dict | None, list[str]]:
    """(normalised request, []) or (None, problems). Unknown keys, wrong types, oversize text and control characters
    are problems; nothing in the text is interpreted."""
    if not isinstance(raw, dict):
        return None, [f"request: expected a mapping, got {type(raw).__name__}"]
    errs = [f"{_shown(k)}: unknown key (allowed: {', '.join(KEYS)})" for k in raw if k not in KEYS]
    errs += [f"{k}: missing" for k in REQUIRED if k not in raw]
    if errs:
        return None, errs
    out: dict[str, Any] = {}
    if raw["schema"] != SCHEMA:
        errs.append(f"schema: expected {SCHEMA}")
    for k in ("project", "character"):
        v = _text(raw[k], k, errs, limit=64)
        if v is not None and not NAME_RE.match(v.replace("-", "_") if k == "project" else v):
            errs.append(f"{k}: {v!r} is not a lowercase id ({NAME_RE.pattern})")
        out[k] = v
    for k in TEXT_KEYS:
        if k in raw:
            out[k] = _text(raw[k], k, errs, limit=256 if k == "display_name" else MAX_TEXT)
    h = raw["height_m"]
    if isinstance(h, bool) or not isinstance(h, (int, float)) or not math.isfinite(h):
        errs.append("height_m: expected a finite number (game standing height in metres)")
    elif not HEIGHT_RANGE[0] <= h <= HEIGHT_RANGE[1]:
        errs.append(f"height_m: {h} is outside {HEIGHT_RANGE[0]}..{HEIGHT_RANGE[1]}")
    else:
        out["height_m"] = float(h)
    out["invariants"] = _texts(raw["invariants"], "invariants", errs)
    clips = _texts(raw["clips"], "clips", errs, nonempty=True)
    if clips is not None and len(set(clips)) != len(clips):
        errs.append("clips: a clip is listed twice")
    out["clips"] = clips
    refs = raw.get("references")
    if refs is not None:
        if not isinstance(refs, dict):
            errs.append("references: expected a mapping")
        else:
            errs += [
                f"references.{_shown(k)}: unknown key (allowed: {', '.join(REF_KEYS)})"
                for k in refs
                if k not in REF_KEYS
            ]
            r: dict[str, Any] = {}
            for k in ("stills_dir", "approved_by"):
                if refs.get(k) is not None:
                    r[k] = _text(refs[k], f"references.{k}", errs)
            p = refs.get("provenance")
            if isinstance(p, list):
                r["provenance"] = _texts(p, "references.provenance", errs)
            elif p is not None:
                r["provenance"] = _text(p, "references.provenance", errs)
            out["references"] = r
    decisions = raw.get("owner_decisions")
    if decisions is not None:
        if not isinstance(decisions, list) or len(decisions) > MAX_ITEMS:
            errs.append(f"owner_decisions: expected a list of at most {MAX_ITEMS} entries")
        else:
            for i, d in enumerate(decisions):
                where = f"owner_decisions[{i}]"
                if not isinstance(d, dict):
                    _text(d, where, errs)
                    continue
                if len(d) > 16:
                    errs.append(f"{where}: more than 16 fields")
                for k, v in d.items():
                    if _text(k, f"{where} key", errs, limit=64) is not None and v is not None:
                        _text(v, f"{where}.{k}", errs)
            out["owner_decisions"] = decisions
    return (None, errs) if errs else (out, [])


def load_request_file(path: Path) -> tuple[Any, str]:
    """(parsed YAML/JSON document, file sha256); refuses files over MAX_FILE before parsing."""
    data = Path(path).read_bytes()
    if len(data) > MAX_FILE:
        raise ValueError(f"{path}: {len(data)} bytes, a class request is at most {MAX_FILE}")
    try:
        doc = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as e:
        raise ValueError(f"{path}: not UTF-8 YAML/JSON ({type(e).__name__})") from None
    return doc, hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- findings

BLOCKS = {
    "request_invalid": "invalid",
    "project_mismatch": "invalid",
    "id_collides_game_sheet": "invalid",
    "id_near_collision": "invalid",
    "registry_collision": "invalid",
    "unknown_clip": "invalid",
    "unsupported_clip": "invalid",
    "missing_still": "needs_reference_preparation",
    "still_unreadable": "needs_reference_preparation",
    "still_size": "needs_reference_preparation",
    "still_foot_row": "needs_reference_preparation",
    "baked_white_border": "needs_reference_preparation",
    "mirrored_independent_still": "needs_reference_preparation",
    "needs_profile_row": "needs_profile_row",
    # the validator's still issues, prefixed (validate.validate check_cell issue codes)
    **{
        f"still_{c}": "needs_reference_preparation"
        for c in ("canvas", "empty", "palette", "fringe", "semi_alpha", "colours", "touches_edge")
    },
}


def _finding(code: str, detail: str, direction: str | None = None) -> dict:
    f = {"code": code, "blocks": BLOCKS[code], "detail": detail}
    if direction is not None:
        f["direction"] = direction
    return f


# ---------------------------------------------------------------- stills


def read_stills(directory: Path | None, order: tuple[str, ...]) -> tuple[dict[str, np.ndarray], dict, list[dict]]:
    """Read still_<direction>.png for the profile directions only: (pixels, {d: file sha256 | None}, unreadable)."""
    stills: dict[str, np.ndarray] = {}
    hashes: dict[str, str | None] = dict.fromkeys(order)
    bad: list[dict] = []
    if directory is None or not Path(directory).is_dir():
        return stills, hashes, bad
    for d in order:
        p = Path(directory) / f"still_{d}.png"
        if p.is_symlink() or not p.is_file():
            continue
        hashes[d] = hashlib.sha256(p.read_bytes()).hexdigest()
        try:
            with Image.open(p) as im:
                if im.format != "PNG" or max(im.size) > MAX_STILL_EDGE:
                    raise ValueError(f"{im.format} {im.size[0]}x{im.size[1]}, expected a PNG cell")
                stills[d] = np.asarray(im.convert("RGBA")).copy()
        except (OSError, ValueError) as e:
            bad.append(_finding("still_unreadable", f"{p.name}: {e}", d))
    return stills, hashes, bad


def _outside(transparent: np.ndarray) -> np.ndarray:
    """Transparent pixels 4-connected to the frame border (the background, not enclosed holes)."""
    out = np.zeros_like(transparent)
    for edge in (np.s_[0, :], np.s_[-1, :], np.s_[:, 0], np.s_[:, -1]):
        out[edge] = transparent[edge]
    while True:
        p = np.pad(out, 1)
        grown = (out | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]) & transparent
        if np.array_equal(grown, out):
            return out
        out = grown


def outer_white_pixels(f: np.ndarray) -> int:
    """Opaque pure-white pixels touching the background or the frame edge: a baked sticker border (the game's
    PixelStandee.gdshader draws that border at runtime; profile outline.baked_border: forbidden)."""
    opaque = f[..., 3] > 0
    if not opaque.any():
        return 0
    bg = np.pad(_outside(~opaque), 1, constant_values=True)
    edge = opaque & (bg[:-2, 1:-1] | bg[2:, 1:-1] | bg[1:-1, :-2] | bg[1:-1, 2:])
    return int((edge & (f[..., :3] == WHITE).all(-1)).sum())


def _same_pixels(a: np.ndarray, b: np.ndarray) -> bool:
    """Equal alpha everywhere and equal RGB where opaque (RGB under alpha 0 is not an image difference)."""
    if a.shape != b.shape or not np.array_equal(a[..., 3], b[..., 3]):
        return False
    m = a[..., 3] > 0
    return bool(np.array_equal(a[m, :3], b[m, :3]))


def _size_ok(size: tuple[int, int]) -> bool:
    return size[0] == size[1] and size[0] % 8 == 0 and CELL_MIN <= size[0] <= CELL_MAX


def still_findings(
    stills: dict[str, np.ndarray], profile: Profile, palette: np.ndarray, character: str
) -> tuple[list[dict], int | None, dict[str, dict]]:
    """Cell-size, validator, foot, border and mirror findings for the stills present: (findings, cell, measurements).
    With a profile row the cell is the row's; without one it is the stills' shared size (the most common one, which
    must be a square multiple of 8 within CELL_MIN..CELL_MAX)."""
    from sonoranlab.validate import validate

    findings: list[dict] = []
    order = profile.directions.order
    row = character in profile.characters
    sizes = {d: (f.shape[1], f.shape[0]) for d, f in stills.items()}
    cell = None
    if row:
        cell = profile.character(character).cell[0]
    elif sizes:
        common = Counter(sizes[d] for d in order if d in sizes).most_common(1)[0][0]
        cell = common[0] if _size_ok(common) else None
        for d in order:
            if d in sizes and (cell is None or sizes[d] != (cell, cell)):
                within = f" (the set's cell is {cell})" if cell else ""
                msg = f"still {d} is {sizes[d][0]}x{sizes[d][1]}; all eight must share one square cell, a multiple"
                findings.append(_finding("still_size", f"{msg} of 8 within {CELL_MIN}..{CELL_MAX}{within}", d))
    measures: dict[str, dict] = {d: {"size": list(sizes[d]), "bbox": None} for d in stills}
    if cell is None:
        return findings, None, measures
    spec = profile.character(character, cell=None if row else cell)
    checked = {d: f for d, f in stills.items() if row or sizes[d] == (cell, cell)}
    report = validate(
        {},
        cell=spec.cell,
        frames=0,
        directions=list(profile.directions.generate),
        palette=palette,
        foot_y=spec.foot_y,
        anchor_x=spec.anchor_x,
        padding=spec.padding,
        limits=spec.limits,
        max_colours=profile.palette.max_colors_per_cell,
        loop=False,
        anchor_mode=spec.anchor_mode,
        fringe_tolerance=profile.alpha.fringe_tolerance,
        mirror=dict(profile.directions.mirror_animations),
        stills=checked,
    )
    for rec in report["stills"]:
        d = rec["direction"]
        f = checked[d]
        measures[d]["bbox"] = rec["bbox"]
        findings += [_finding("still_" + issue.split(":", 1)[0], issue, d) for issue in rec["issues"]]
        if rec["bbox"] is not None and rec["canvas"] == list(spec.cell) and rec["bbox"][3] != spec.foot_y:
            msg = f"still {d} lowest opaque row {rec['bbox'][3]}, expected foot_y {spec.foot_y} (cell - 4)"
            findings.append(_finding("still_foot_row", msg, d))
        if n := outer_white_pixels(f):
            msg = (
                f"still {d} has {n} opaque #ffffff pixels on the silhouette's outer edge: a baked sticker border "
                "(the game's shader draws it; outline.baked_border: forbidden)"
            )
            findings.append(_finding("baked_white_border", msg, d))
    for dst, src in profile.directions.mirror_animations.items():
        if dst in stills and src in stills and _same_pixels(stills[dst], stills[src][:, ::-1]):
            msg = (
                f"still {dst} is an exact horizontal mirror of still {src}; stills are independent poses (ADR-0007), "
                "only the west animation rows are mirrored"
            )
            findings.append(_finding("mirrored_independent_still", msg, dst))
    return findings, cell, measures


# ---------------------------------------------------------------- registry (read-only)


def read_registry(state: Path | None, project: str, character: str) -> dict | None:
    """The registry record summary for project/character under STATE, read without locking or writing; None if the
    character was never registered. Refuses symlinked or non-regular records."""
    if state is None:
        return None
    root = Path(state)
    path = root / "registry" / project / character / "character.json"
    for part in (path, *path.parents):
        if part == root or not part.is_relative_to(root):
            break
        if part.is_symlink():
            raise ValueError(f"{part}: symlink in registry state")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"{path}: not a regular registry record")
    doc = json.loads(path.read_text())
    sets = doc.get("reference_sets") if isinstance(doc, dict) else None
    if not isinstance(sets, list) or not all(isinstance(s, dict) and isinstance(s.get("files"), dict) for s in sets):
        raise ValueError(f"{path}: corrupt registry record")
    return {
        "reference_sets": [
            {"ref_set_id": s.get("ref_set_id"), "hashes": {d: f.get("sha256") for d, f in s["files"].items()}}
            for s in sets
        ]
    }


# ---------------------------------------------------------------- check


def propose_row(profile: Profile, character: str, cell: int, request_hash: str) -> str:
    """A reviewable `characters.<id>` YAML snippet: the cell, anchor_mode foot and the profile's `validator:` default
    limits copied explicitly. Nothing here edits the profile."""
    if not NAME_RE.match(character):
        raise ValueError(f"character {character!r} is not a profile id")
    if not _size_ok((cell, cell)):
        raise ValueError(f"cell {cell}: expected a multiple of 8 within {CELL_MIN}..{CELL_MAX}")
    lim = profile.validator.defaults
    row = {
        character: {
            "cell": cell,
            "anchor_mode": "foot",
            "max_foot_slide_px": lim.max_foot_slide_px,
            "max_bbox_drift_px": lim.max_bbox_drift_px,
            "opaque_area_cv_warn": lim.opaque_area_cv_warn,
            "loop_closure_ratio_warn": lim.loop_closure_ratio_warn,
        }
    }
    label = profile.validator.label.splitlines()[0][:100]
    head = (
        f"# proposed characters.{character} row for class request sha256 {request_hash}\n"
        f"# limits copied from the profile `validator:` defaults ({label})\n"
        "# owner review + PR before any campaign uses it; sonoranlab never edits the profile\n"
    )
    return head + yaml.safe_dump(row, sort_keys=False)


def _status(findings: list[dict]) -> str:
    blocked = {f["blocks"] for f in findings}
    return next((s for s in STATUSES if s in blocked), "ready")


def check(
    raw: Any,
    profile: Profile,
    palette: np.ndarray,
    *,
    stills_dir: Path | None = None,
    stills_source: str | None = None,
    registry: dict | None = None,
) -> tuple[dict, dict[str, np.ndarray]]:
    """The admission result for a raw request document, and the stills it read (for a contact sheet)."""
    order = profile.directions.order
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "status": "invalid",
        "request_sha256": request_sha256(raw),
        "profile": {"id": profile.id, "sha256": hashlib.sha256(profile.path.read_bytes()).hexdigest()},
        "character": None,
        "cell": None,
        "foot_y": None,
        "profile_row": None,
        "stills": {"source": stills_source, "files": dict.fromkeys(order), "measurements": {}},
        "registry": None,
        "proposed_row": None,
        "findings": [],
    }
    req, problems = parse_request(raw)
    if req is None:
        result["findings"] = [_finding("request_invalid", p) for p in problems]
        return result, {}
    char = req["character"]
    result["character"] = char
    findings: list[dict] = []
    if req["project"] != profile.id:
        findings.append(_finding("project_mismatch", f"request project {req['project']!r}, profile {profile.id!r}"))
    shipped = set(profile.references.style_refs)
    if char in shipped:
        findings.append(_finding("id_collides_game_sheet", f"{char!r} is a shipped game sheet (references.style_refs)"))
    for name in sorted(set(profile.characters) | shipped):
        if name != char and name.replace("_", "") == char.replace("_", ""):
            findings.append(_finding("id_near_collision", f"{char!r} differs from existing {name!r} only in '_'"))
    row = char in profile.characters
    result["profile_row"] = row
    if registry is not None:
        result["registry"] = {**registry, "matching_ref_set": None}
        if not row:
            msg = (
                f"{char!r} has {len(registry['reference_sets'])} registered reference set(s) in STATE but no profile "
                "row: a retired or different character owns this id"
            )
            findings.append(_finding("registry_collision", msg))
    anims = profile.character(char).animations if row else profile.animations
    for clip in req["clips"]:
        if clip not in anims:
            findings.append(_finding("unknown_clip", f"{clip!r} is not a profile animation ({', '.join(anims)})"))
        elif anims[clip].track_default not in SUPPORTED_TRACKS:
            msg = f"{clip!r} has track_default {anims[clip].track_default}; Phase 5 supports Track E clips (run, idle)"
            findings.append(_finding("unsupported_clip", msg))
    stills, hashes, unreadable = read_stills(stills_dir, order)
    result["stills"]["files"] = hashes
    findings += unreadable
    findings += [_finding("missing_still", f"still_{d}.png is missing", d) for d in order if hashes[d] is None]
    found, cell, measures = still_findings(stills, profile, palette, char)
    findings += found
    result["stills"]["measurements"] = measures
    if registry is not None:
        hit = [s["ref_set_id"] for s in registry["reference_sets"] if s["hashes"] == hashes]
        result["registry"]["matching_ref_set"] = hit[0] if hit else None
    result["cell"] = cell
    if cell is not None:
        result["foot_y"] = profile.character(char, cell=None if row else cell).foot_y
    if not row:
        detail = f"no characters.{char} row in profile {profile.id}"
        if cell is not None:
            result["proposed_row"] = propose_row(profile, char, cell, result["request_sha256"])
            detail += f"; proposed row (cell {cell}) attached for owner review and a PR"
        else:
            detail += "; sizing a row needs a consistent still set first (supply stills or run refprep)"
        findings.append(_finding("needs_profile_row", detail))
    result["findings"] = findings
    result["status"] = _status(findings)
    return result, stills


# ---------------------------------------------------------------- contact


def contact(result: dict, stills: dict[str, np.ndarray], order: tuple[str, ...], *, scale: int = 4) -> Image.Image:
    """The eight stills (blank placeholders for missing ones) at `scale`x nearest, one row per direction, with
    direction labels and finding badges. contact.contact_sheet without the sticker border, so baked pixels show."""
    from sonoranlab.contact import contact_sheet

    cell = result.get("cell") or max([CELL_MIN, *(max(f.shape[:2]) for f in stills.values())])
    clips, frames = {}, []
    for i, d in enumerate(order):
        tile = np.zeros((cell, cell, 4), np.uint8)
        if d in stills:
            f = stills[d][:cell, :cell]
            tile[: f.shape[0], : f.shape[1]] = f
        clips[d] = [tile]
        issues = [f"{f['code']}: {f['detail']}" for f in result["findings"] if f.get("direction") == d]
        frames.append({"direction": d, "frame": 0, "index": i, "ok": not issues, "issues": issues})
    report = {
        "pass": result["status"] == "ready",
        "character": result.get("character") or "?",
        "animation": result["status"],
        "failures": [f"{f['code']}: {f['detail']}" for f in result["findings"]],
        "warnings": [],
        "frames": frames,
    }
    return contact_sheet(clips, report, cell=(cell, cell), foot_y=cell - 4, scale=scale, border=False, rows=order)
