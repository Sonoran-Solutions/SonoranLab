"""Load and check a project profile (docs/07): a strict schema with specific errors and no side effects.

Relative paths in a profile resolve against the profile file's directory; `~` expands. `load_profile` checks the
schema and the pose templates the profile points at. `check_profile(check_paths=True)` also checks the paths outside
the repo: the palette file (exists, sha256, colour count, bg_key absent) and the export project and manifest.
Every problem names the field and the bad value. Nothing here writes a file or imports torch.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import yaml

ANCHOR_MODES = ("foot", "rig", "bbox_bottom")
RIG_UNSUPPORTED = "rig anchors need Track D per-frame anchors and a rig validator rule; not supported in Phase 2"
TRACKS = ("A", "B", "C", "D", "E")
LIMIT_KEYS = ("max_foot_slide_px", "max_bbox_drift_px", "opaque_area_cv_warn", "loop_closure_ratio_warn")
HOVER_KEYS = ("hover_x_drift_warn_px", "hover_min_bob_px")  # ADR-0008 F2: required for anchor_mode bbox_bottom
PROCESSING_VERSIONS = ("processing-v1",)  # benchmark/characters/2026-10-18-processing-v1.yaml
DOWNSCALERS = ("k_centroid", "contrast", "nearest")  # sonoranlab.pixelize methods
NAME_RE = re.compile(r"^[a-z0-9_]+$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
CELL_MINUS_RE = re.compile(r"^cell_minus_(\d+)$")
_MISSING = object()
TOP_KEYS = (
    "id",
    "name",
    "style",
    "canvas",
    "palette",
    "anchor",
    "alpha",
    "outline",
    "directions",
    "animations",
    "validator",
    "characters",
    "references",
    "export",
    "autonomy",
    "processing",
)


class ProfileError(ValueError):
    """A profile that fails its schema or path checks; `problems` holds one message per problem."""

    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Limits:
    """Validator limits: two gates (px) and two warnings (ratios); bbox_bottom adds two hover warnings (px)."""

    max_foot_slide_px: float
    max_bbox_drift_px: float
    opaque_area_cv_warn: float
    loop_closure_ratio_warn: float
    hover_x_drift_warn_px: float | None = None  # warn: core centre x range over the loop above this
    hover_min_bob_px: float | None = None  # warn: centroid y range over the loop below this


@dataclass(frozen=True)
class Animation:
    frames: int
    fps: float
    loop: bool
    loop_from: int | None = None  # non-looping clip ping-pongs over loop_from..frames-1 (the game's channel)
    hold_last: bool = False
    track_default: str | None = None
    template: Path | None = None  # templates/poses/<name> dir holding template.yaml


@dataclass(frozen=True)
class Canvas:
    cell: tuple[int, int] | None  # (w, h); None = per_character
    gen_scale: int
    padding: int
    sheet_layout: str
    density_px_per_m: float | None = None


@dataclass(frozen=True)
class Palette:
    source: Path
    sha256: str
    colours: int
    max_colors_per_cell: int
    dither: str


@dataclass(frozen=True)
class Anchor:
    mode: str
    foot_y: str | int  # "cell_minus_N" or a row
    anchor_x: str | int  # "cell_half" or a column
    registration: str
    foot_zone: tuple[int, int] | None = None


@dataclass(frozen=True)
class Alpha:
    threshold: int
    bg_key: str
    fringe_passes: int
    fringe_tolerance: int = 0


@dataclass(frozen=True)
class Processing:
    """The per-frame chain's frozen parameters (`sonoranlab process`); scale is inferred per input, not set here."""

    version: str
    downscaler: str
    thickness: int  # PixelOE outline expansion, HD px
    despill_px: int  # magenta despill band, HD px
    key_tol: float  # chroma-key RGB distance for inputs without alpha


@dataclass(frozen=True)
class Outline:
    colour: str
    baked_border: str


@dataclass(frozen=True)
class Directions:
    order: tuple[str, ...]
    generate: tuple[str, ...]
    mirror_animations: dict[str, str]  # mirrored row -> generated source row
    mirror_right_from_left: bool = False  # docs/07 spelling; superseded by mirror_animations


@dataclass(frozen=True)
class Validator:
    label: str  # where the defaults come from; shown on every character that uses them
    defaults: Limits


@dataclass(frozen=True)
class CharacterRow:
    """A `characters:` entry as written; animations are already merged with the profile's."""

    cell: tuple[int, int] | None
    anchor_mode: str | None
    limits: Limits
    animations: dict[str, Animation]
    locomotion: str | None


@dataclass(frozen=True)
class References:
    style_refs: tuple[str, ...] = ()
    proportions: str | None = None
    outline: str | None = None
    camera: str | None = None
    vfx: str | None = None


@dataclass(frozen=True)
class Export:
    godot_project: Path
    sprites_dir: str
    manifest: str


@dataclass(frozen=True)
class Autonomy:
    allow_auto_approve: bool
    max_seeds_per_attempt: int
    max_attempts_per_animation: int
    escalate_after: str


@dataclass(frozen=True)
class CharacterSpec:
    """Everything the chain needs for one character, resolved from its row or the labelled defaults."""

    name: str
    cell: tuple[int, int]  # (w, h)
    anchor_mode: str
    foot_y: int  # row the clip's lowest opaque row registers to
    anchor_x: int  # column the clip's median bbox centre registers to
    padding: int
    limits: Limits
    limits_source: str
    uses_defaults: bool  # True: not in `characters:`, limits are the validator defaults
    animations: dict[str, Animation]
    locomotion: str | None  # clip that stands in for "the walk" (ADR-0007)


@dataclass(frozen=True)
class Profile:
    path: Path
    id: str
    name: str
    style: str
    canvas: Canvas
    palette: Palette
    anchor: Anchor
    alpha: Alpha
    outline: Outline | None
    directions: Directions
    animations: dict[str, Animation]
    validator: Validator
    characters: dict[str, CharacterRow]
    references: References
    export: Export
    autonomy: Autonomy
    processing: Processing | None = None  # optional for the schema; `sonoranlab process` refuses a profile without it

    def character(self, name: str, *, cell: int | list[int] | tuple[int, int] | None = None) -> CharacterSpec:
        """Resolve `name`. A character without a row gets the validator defaults (and says so); it needs `cell`
        when the canvas is per_character."""
        where = f"characters.{name}"
        if not NAME_RE.match(name):
            raise ProfileError([f"character {name!r}: name must match {NAME_RE.pattern}"])
        want = None
        if cell is not None:
            errs: list[str] = []
            want = _cell(cell, "cell", errs)
            if errs:
                raise ProfileError(errs)
        row = self.characters.get(name)
        if row:
            size = row.cell or self.canvas.cell
            if want and want != size:
                raise ProfileError([f"{where}.cell: profile says {list(size)}, caller asked for {list(want)}"])
            limits, source, defaults, anims = row.limits, where, False, row.animations
        else:
            size = want or self.canvas.cell
            if size is None:
                raise ProfileError([f"{where}: not in `characters:` and canvas.cell is per_character; pass its cell"])
            limits, source, defaults, anims = self.validator.defaults, self.validator.label, True, self.animations
        errs = []
        foot_y, anchor_x = _anchor_rows(self.anchor, size, self.canvas.padding, where, errs)
        if errs:
            raise ProfileError(errs)
        loco = row.locomotion if row and row.locomotion else ("run" if "run" in anims else None)
        return CharacterSpec(
            name=name,
            cell=size,
            anchor_mode=(row.anchor_mode if row else None) or self.anchor.mode,
            foot_y=foot_y,
            anchor_x=anchor_x,
            padding=self.canvas.padding,
            limits=limits,
            limits_source=source,
            uses_defaults=defaults,
            animations=anims,
            locomotion=loco,
        )


def load_profile(path: str | Path, *, check_paths: bool = False) -> Profile:
    """Parse and check `path`; raise ProfileError listing every problem."""
    profile, errs = _parse(Path(path), check_paths)
    if errs or profile is None:
        raise ProfileError(errs)
    return profile


def check_profile(path: str | Path, *, check_paths: bool = True) -> list[str]:
    """Every problem with the profile at `path` (empty = OK)."""
    return _parse(Path(path), check_paths)[1]


def load_palette(profile: Profile) -> np.ndarray:
    """The palette as sorted unique RGB rows (N, 3) uint8. Refuses a sha256 or colour-count mismatch and a palette
    that contains alpha.bg_key."""
    pal, errs = _read_palette(profile.palette, profile.alpha.bg_key)
    if errs or pal is None:
        raise ProfileError(errs)
    return pal


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- parsing


def _parse(path: Path, check_paths: bool) -> tuple[Profile | None, list[str]]:
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        return None, [f"{path}: no such file"]
    except yaml.YAMLError as e:
        return None, [f"{path}: not valid YAML: {e}"]
    if not isinstance(raw, dict):
        return None, [f"{path}: top level must be a mapping, got {type(raw).__name__}"]
    base = path.resolve().parent
    errs: list[str] = []
    _unknown(raw, "", TOP_KEYS, errs)
    pid = _val(raw, "", "id", "name", errs)
    name = _val(raw, "", "name", "str", errs)
    style = _val(raw, "", "style", "str", errs, choices=("pixel",))

    c = _section(raw, "canvas", ("cell", "gen_scale", "padding", "sheet_layout", "density_px_per_m"), errs)
    canvas = None
    if c is not None:
        cell = c.get("cell", _MISSING)
        if cell is _MISSING:
            errs.append("canvas.cell: missing")
        canvas = Canvas(
            cell=None if cell in ("per_character", _MISSING) else _cell(cell, "canvas.cell", errs),
            gen_scale=_val(c, "canvas", "gen_scale", "int", errs, lo=1),
            padding=_val(c, "canvas", "padding", "int", errs, lo=0),
            sheet_layout=_val(c, "canvas", "sheet_layout", "str", errs, choices=("rows_by_direction", "strip")),
            density_px_per_m=_val(c, "canvas", "density_px_per_m", "num", errs, gt=0, required=False),
        )

    p = _section(raw, "palette", ("source", "sha256", "colours", "max_colors_per_cell", "dither"), errs)
    palette = None
    if p is not None:
        sha = _val(p, "palette", "sha256", "str", errs)
        if sha is not None and not re.fullmatch(r"[0-9a-f]{64}", sha):
            errs.append(f"palette.sha256: {sha!r} is not 64 lowercase hex digits")
        dither = _val(p, "palette", "dither", "str", errs)
        if dither is not None and dither != "none":
            errs.append(f"palette.dither: {dither!r} is not supported (only 'none'; dithering is not implemented)")
        palette = Palette(
            source=_path(_val(p, "palette", "source", "str", errs), base),
            sha256=sha,
            colours=_val(p, "palette", "colours", "int", errs, lo=1),
            max_colors_per_cell=_val(p, "palette", "max_colors_per_cell", "int", errs, lo=1),
            dither=dither,
        )

    a = _section(raw, "anchor", ("mode", "foot_y", "anchor_x", "registration", "foot_zone"), errs)
    anchor = None
    if a is not None:
        foot_y = _val(a, "anchor", "foot_y", "any", errs)
        if foot_y is not None and not (_is_int(foot_y) or (isinstance(foot_y, str) and CELL_MINUS_RE.match(foot_y))):
            errs.append(f"anchor.foot_y: {foot_y!r} is neither a row nor cell_minus_<N>")
        anchor_x = _val(a, "anchor", "anchor_x", "any", errs)
        if anchor_x is not None and not (_is_int(anchor_x) or anchor_x == "cell_half"):
            errs.append(f"anchor.anchor_x: {anchor_x!r} is neither a column nor cell_half")
        zone = a.get("foot_zone")
        if zone is not None and not (
            isinstance(zone, list) and len(zone) == 2 and all(_is_int(v) for v in zone) and zone[0] <= zone[1]
        ):
            errs.append(f"anchor.foot_zone: {zone!r} is not [first_row, last_row]")
            zone = None
        anchor = Anchor(
            mode=_val(a, "anchor", "mode", "str", errs, choices=ANCHOR_MODES),
            foot_y=foot_y,
            anchor_x=anchor_x,
            registration=_val(a, "anchor", "registration", "str", errs, choices=("per_clip",)),
            foot_zone=tuple(zone) if zone else None,
        )
        if anchor.mode == "rig":
            errs.append(f"anchor.mode: {RIG_UNSUPPORTED}")

    al = _section(raw, "alpha", ("threshold", "bg_key", "fringe_passes", "fringe_tolerance"), errs)
    alpha = None
    if al is not None:
        alpha = Alpha(
            threshold=_val(al, "alpha", "threshold", "int", errs, lo=1, hi=255),
            bg_key=_val(al, "alpha", "bg_key", "hex", errs),
            fringe_passes=_val(al, "alpha", "fringe_passes", "int", errs, lo=0),
            fringe_tolerance=_val(al, "alpha", "fringe_tolerance", "int", errs, lo=0, required=False, default=0),
        )

    o = _section(raw, "outline", ("colour", "baked_border"), errs, required=False)
    outline = None
    if o is not None:
        outline = Outline(
            colour=_val(o, "outline", "colour", "hex", errs),
            baked_border=_val(o, "outline", "baked_border", "str", errs, choices=("forbidden", "allowed")),
        )

    directions = _directions(raw, errs)
    generate = directions.generate if directions else ()
    templates: dict[Path, dict | None] = {}
    animations = _animations(raw, generate, templates, base, errs)
    validator = None
    v = _section(raw, "validator", ("label", *LIMIT_KEYS, *HOVER_KEYS), errs)
    if v is not None:
        hover = bool(anchor and anchor.mode == "bbox_bottom")
        validator = Validator(
            label=_val(v, "validator", "label", "str", errs), defaults=_limits(v, "validator", errs, hover=hover)
        )
    characters = _characters(raw, canvas, anchor, animations, generate, templates, base, errs)

    r = _section(raw, "references", ("style_refs", "proportions", "outline", "camera", "vfx"), errs, required=False)
    references = References()
    if r is not None:
        refs = r.get("style_refs", [])
        if not (isinstance(refs, list) and all(isinstance(s, str) for s in refs)):
            errs.append(f"references.style_refs: expected a list of names/paths, got {refs!r}")
            refs = []
        texts = {k: _val(r, "references", k, "str", errs, required=False) for k in ("proportions", "outline", "camera")}
        references = References(tuple(refs), vfx=_val(r, "references", "vfx", "str", errs, required=False), **texts)

    e = _section(raw, "export", ("godot_project", "sprites_dir", "manifest"), errs)
    export = None
    if e is not None:
        sprites = _val(e, "export", "sprites_dir", "str", errs)
        if sprites is not None and not sprites.startswith("res://"):
            errs.append(f"export.sprites_dir: {sprites!r} is not a res:// path")
        export = Export(
            godot_project=_path(_val(e, "export", "godot_project", "str", errs), base),
            sprites_dir=sprites,
            manifest=_val(e, "export", "manifest", "str", errs),
        )

    keys = ("allow_auto_approve", "max_seeds_per_attempt", "max_attempts_per_animation", "escalate_after")
    au = _section(raw, "autonomy", keys, errs)
    autonomy = None
    if au is not None:
        autonomy = Autonomy(
            allow_auto_approve=_val(au, "autonomy", "allow_auto_approve", "bool", errs),
            max_seeds_per_attempt=_val(au, "autonomy", "max_seeds_per_attempt", "int", errs, lo=1),
            max_attempts_per_animation=_val(au, "autonomy", "max_attempts_per_animation", "int", errs, lo=1),
            escalate_after=_val(
                au, "autonomy", "escalate_after", "str", errs, choices=("ask", "switch_track", "give_up")
            ),
        )

    pr = _section(
        raw, "processing", ("version", "downscaler", "thickness", "despill_px", "key_tol"), errs, required=False
    )
    processing = None
    if pr is not None:
        processing = Processing(
            version=_val(pr, "processing", "version", "str", errs, choices=PROCESSING_VERSIONS),
            downscaler=_val(pr, "processing", "downscaler", "str", errs, choices=DOWNSCALERS),
            thickness=_val(pr, "processing", "thickness", "int", errs, lo=0, hi=6),
            despill_px=_val(pr, "processing", "despill_px", "int", errs, lo=0),
            key_tol=_val(pr, "processing", "key_tol", "num", errs, lo=0),
        )
        if processing.despill_px and alpha and alpha.bg_key and alpha.bg_key.lower() != "#ff00ff":
            errs.append(
                f"processing.despill_px: {processing.despill_px} needs alpha.bg_key #ff00ff "
                f"(despill is magenta-only), got {alpha.bg_key}"
            )

    if canvas and anchor and canvas.cell is not None:
        _anchor_rows(anchor, canvas.cell, canvas.padding or 0, "canvas", errs)
    if check_paths:
        if palette and palette.source and alpha and alpha.bg_key:
            errs += _read_palette(palette, alpha.bg_key)[1]
        if export and export.godot_project:
            errs += _check_export(export)
    if errs:
        return None, errs
    profile = Profile(
        path=path,
        id=pid,
        name=name,
        style=style,
        canvas=canvas,
        palette=palette,
        anchor=anchor,
        alpha=alpha,
        outline=outline,
        directions=directions,
        animations=animations,
        validator=validator,
        characters=characters,
        references=references,
        export=export,
        autonomy=autonomy,
        processing=processing,
    )
    return profile, []


def _directions(raw: dict, errs: list[str]) -> Directions | None:
    d = _section(raw, "directions", ("order", "generate", "mirror_animations", "mirror_right_from_left"), errs)
    if d is None:
        return None
    lists = {}
    for key in ("order", "generate"):
        v = d.get(key)
        if not (isinstance(v, list) and v and all(isinstance(s, str) for s in v)):
            errs.append(f"directions.{key}: expected a non-empty list of direction names, got {v!r}")
            v = []
        elif len(set(v)) != len(v):
            errs.append(f"directions.{key}: {v!r} repeats a direction")
        lists[key] = v
    order, generate = lists["order"], lists["generate"]
    mirror = d.get("mirror_animations", {})
    if not (isinstance(mirror, dict) and all(isinstance(k, str) and isinstance(s, str) for k, s in mirror.items())):
        errs.append(f"directions.mirror_animations: expected {{mirrored: source}} names, got {mirror!r}")
        mirror = {}
    if order:
        for g in generate:
            if g not in order:
                errs.append(f"directions.generate: {g!r} is not in directions.order")
        for dst, src in mirror.items():
            if dst not in order:
                errs.append(f"directions.mirror_animations: {dst!r} is not in directions.order")
            if src not in generate:
                errs.append(f"directions.mirror_animations.{dst}: source {src!r} is not generated")
            if dst in generate:
                errs.append(f"directions.mirror_animations: {dst!r} is both generated and mirrored")
        for o in order:
            if o not in generate and o not in mirror:
                errs.append(f"directions.order: {o!r} is neither generated nor mirrored")
    mrl = _val(d, "directions", "mirror_right_from_left", "bool", errs, required=False, default=False)
    return Directions(tuple(order), tuple(generate), dict(mirror), mrl)


ANIM_KEYS = ("frames", "fps", "loop", "loop_from", "hold_last", "track_default", "template")


def _animation(a: Any, where: str, base: Path, errs: list[str], *, parent: Animation | None = None) -> Animation | None:
    """Parse one clip; with `parent`, `a` is a partial override of it."""
    if not isinstance(a, dict):
        errs.append(f"{where}: expected a mapping, got {a!r}")
        return None
    _unknown(a, where, ANIM_KEYS, errs)
    req = parent is None
    vals: dict[str, Any] = {
        "frames": _val(a, where, "frames", "int", errs, lo=1, required=req),
        "fps": _val(a, where, "fps", "num", errs, gt=0, required=req),
        "loop": _val(a, where, "loop", "bool", errs, required=req),
        "loop_from": _val(a, where, "loop_from", "int", errs, lo=0, required=False),
        "hold_last": _val(a, where, "hold_last", "bool", errs, required=False),
        "track_default": _val(a, where, "track_default", "str", errs, choices=TRACKS, required=False),
        "template": _path(_val(a, where, "template", "str", errs, required=False), base),
    }
    vals = {
        k: v for k, v in vals.items() if k in a and (v is not None or k in ("loop_from", "track_default", "template"))
    }
    if req and any(k not in vals for k in ("frames", "fps", "loop")):
        return None
    anim = replace(parent, **vals) if parent else Animation(**vals)
    if anim.loop_from is not None and (anim.loop or anim.loop_from >= anim.frames):
        errs.append(f"{where}.loop_from: {anim.loop_from} needs loop: false and < frames ({anim.frames})")
    if anim.hold_last and anim.loop:
        errs.append(f"{where}.hold_last: needs loop: false")
    return anim


def _animations(raw: dict, generate: tuple, templates: dict, base: Path, errs: list[str]) -> dict[str, Animation]:
    a = raw.get("animations")
    if not isinstance(a, dict) or not a:
        errs.append(f"animations: expected a non-empty mapping of clips, got {a!r}")
        return {}
    out = {}
    for name, spec in a.items():
        anim = _animation(spec, f"animations.{name}", base, errs)
        if anim:
            _check_template(anim, f"animations.{name}", generate, templates, errs)
            out[name] = anim
    return out


def _characters(
    raw: dict,
    canvas: Canvas | None,
    anchor: Anchor | None,
    animations: dict[str, Animation],
    generate: tuple,
    templates: dict,
    base: Path,
    errs: list[str],
) -> dict[str, CharacterRow]:
    chars = raw.get("characters", {}) or {}
    if not isinstance(chars, dict):
        errs.append(f"characters: expected a mapping of name -> row, got {chars!r}")
        return {}
    out = {}
    keys = ("cell", "anchor_mode", "animations", "locomotion", *LIMIT_KEYS, *HOVER_KEYS)
    for name, row in chars.items():
        where = f"characters.{name}"
        if not (isinstance(name, str) and NAME_RE.match(name)):
            errs.append(f"{where}: name must match {NAME_RE.pattern}")
            continue
        if not isinstance(row, dict):
            errs.append(f"{where}: expected a mapping, got {row!r}")
            continue
        _unknown(row, where, keys, errs)
        cell = _cell(row["cell"], f"{where}.cell", errs) if "cell" in row else None
        if cell is None and "cell" not in row and canvas and canvas.cell is None:
            errs.append(f"{where}.cell: missing (canvas.cell is per_character)")
        anims = dict(animations)
        over = row.get("animations", {})
        if not isinstance(over, dict):
            errs.append(f"{where}.animations: expected a mapping of overrides, got {over!r}")
            over = {}
        for clip, spec in over.items():
            if clip not in animations:
                errs.append(f"{where}.animations.{clip}: not a clip in animations ({', '.join(animations)})")
                continue
            anim = _animation(spec, f"{where}.animations.{clip}", base, errs, parent=animations[clip])
            if anim:
                if {"frames", "template"} & set(spec):
                    _check_template(anim, f"{where}.animations.{clip}", generate, templates, errs)
                anims[clip] = anim
        loco = _val(row, where, "locomotion", "str", errs, required=False)
        if loco is not None and loco not in anims:
            errs.append(f"{where}.locomotion: {loco!r} is not a clip in animations")
        size = cell or (canvas.cell if canvas else None)
        if anchor and canvas and size:
            _anchor_rows(anchor, size, canvas.padding or 0, where, errs)
        mode = _val(row, where, "anchor_mode", "str", errs, choices=ANCHOR_MODES, required=False)
        if mode == "rig":
            errs.append(f"{where}.anchor_mode: {RIG_UNSUPPORTED}")
        out[name] = CharacterRow(
            cell=cell,
            anchor_mode=mode,
            limits=_limits(row, where, errs, hover=(mode or (anchor.mode if anchor else None)) == "bbox_bottom"),
            animations=anims,
            locomotion=loco,
        )
    return out


def _limits(d: dict, where: str, errs: list[str], *, hover: bool = False) -> Limits:
    """LIMIT_KEYS are always required; HOVER_KEYS when the row's anchor_mode is bbox_bottom (ADR-0008 F2)."""
    vals = {}
    for k in LIMIT_KEYS + HOVER_KEYS:
        if k in LIMIT_KEYS and k not in d:
            errs.append(f"{where}.{k}: missing (no validator threshold may be unset)")
        elif hover and k not in d:
            errs.append(f"{where}.{k}: missing (anchor_mode bbox_bottom needs the hover warnings, ADR-0008 F2)")
        vals[k] = _val(d, where, k, "num", errs, lo=0, gt=0 if k in LIMIT_KEYS[:2] else None, required=False)
    return Limits(**vals)


def _anchor_rows(anchor: Anchor, cell: tuple[int, int], padding: int, where: str, errs: list[str]) -> tuple[int, int]:
    """(foot_y, anchor_x) for a (w, h) cell; records foot rows outside the cell/padding or foot_zone."""
    w, h = cell
    m = CELL_MINUS_RE.match(anchor.foot_y) if isinstance(anchor.foot_y, str) else None
    foot_y = h - int(m.group(1)) if m else anchor.foot_y
    anchor_x = w // 2 if anchor.anchor_x == "cell_half" else anchor.anchor_x
    if not isinstance(foot_y, int) or not isinstance(anchor_x, int):
        return -1, -1
    if not padding <= foot_y <= h - 1 - padding:
        errs.append(
            f"{where}: anchor.foot_y {anchor.foot_y!r} puts the foot line on row {foot_y}, outside rows "
            f"{padding}..{h - 1 - padding} of the {w}x{h} cell (padding {padding})"
        )
    if anchor.foot_zone and not anchor.foot_zone[0] <= foot_y <= anchor.foot_zone[1]:
        errs.append(f"{where}: anchor.foot_y row {foot_y} is outside anchor.foot_zone {list(anchor.foot_zone)}")
    if not padding <= anchor_x <= w - 1 - padding:
        errs.append(f"{where}: anchor.anchor_x {anchor.anchor_x!r} is column {anchor_x}, outside the {w}px cell")
    return foot_y, anchor_x


def _check_template(anim: Animation, where: str, generate: tuple, cache: dict, errs: list[str]) -> None:
    if anim.template is None:
        return
    f = anim.template / "template.yaml"
    if f not in cache:
        try:
            cache[f] = yaml.safe_load(f.read_text())
        except (OSError, yaml.YAMLError):
            cache[f] = None
    t = cache[f]
    if not isinstance(t, dict):
        errs.append(f"{where}.template: {anim.template} has no readable template.yaml")
        return
    sets = sorted((t.get("frame_sets") or {}).keys())
    if anim.frames not in sets:
        errs.append(f"{where}.frames: {anim.frames} does not match template {anim.template} (frame sets: {sets})")
    missing = [d for d in generate if d not in (t.get("directions") or [])]
    if missing:
        errs.append(f"{where}.template: {anim.template} has no poses for generated directions {missing}")


def _read_palette(p: Palette, bg_key: str) -> tuple[np.ndarray | None, list[str]]:
    from PIL import Image

    if not p.source.is_file():
        return None, [f"palette.source: {p.source} does not exist"]
    got = sha256_file(p.source)
    if got != p.sha256:
        return None, [f"palette.sha256: {p.source} hashes to {got}, profile says {p.sha256}"]
    pal = np.unique(np.asarray(Image.open(p.source).convert("RGB")).reshape(-1, 3), axis=0)
    errs = []
    if len(pal) != p.colours:
        errs.append(f"palette.colours: {p.colours}, but {p.source} has {len(pal)} unique colours")
    key = np.array([int(bg_key[i : i + 2], 16) for i in (1, 3, 5)], np.uint8)
    if (pal == key).all(1).any():
        errs.append(f"alpha.bg_key: {bg_key} is a palette colour; the generation background must be absent")
    return pal, errs


def _check_export(e: Export) -> list[str]:
    if not e.godot_project.is_dir():
        return [f"export.godot_project: {e.godot_project} does not exist"]
    if e.sprites_dir is None or e.manifest is None:
        return []
    sprites = e.godot_project / e.sprites_dir.removeprefix("res://")
    if not sprites.is_dir():
        return [f"export.sprites_dir: {e.sprites_dir} ({sprites}) does not exist"]
    if not (sprites / e.manifest).is_file():
        return [f"export.manifest: {sprites / e.manifest} does not exist"]
    return []


# ---------------------------------------------------------------- primitives


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _path(v: str | None, base: Path) -> Path | None:
    return None if v is None else Path(os.path.normpath(base / Path(v).expanduser()))


def _cell(v: Any, field: str, errs: list[str]) -> tuple[int, int] | None:
    if _is_int(v) and v > 0:
        return (v, v)
    if isinstance(v, list) and len(v) == 2 and all(_is_int(x) and x > 0 for x in v):
        return (v[0], v[1])
    errs.append(f"{field}: {v!r} is not a positive size, [w, h] or per_character")
    return None


def _section(raw: dict, key: str, keys: tuple, errs: list[str], *, required: bool = True) -> dict | None:
    v = raw.get(key)
    if v is None:
        if required:
            errs.append(f"{key}: missing section")
        return None
    if not isinstance(v, dict):
        errs.append(f"{key}: expected a mapping, got {v!r}")
        return None
    _unknown(v, key, keys, errs)
    return v


def _unknown(d: dict, where: str, keys: tuple | list, errs: list[str]) -> None:
    for k in d:
        if k not in keys:
            errs.append(f"{where + '.' if where else ''}{k}: unknown key (allowed: {', '.join(keys)})")


def _val(
    d: dict,
    where: str,
    key: str,
    kind: str,
    errs: list[str],
    *,
    required: bool = True,
    default: Any = None,
    lo: float | None = None,
    hi: float | None = None,
    gt: float | None = None,
    choices: tuple | None = None,
) -> Any:
    """d[key] checked as kind (int|num|bool|str|name|hex|any); records a problem and returns `default` if bad."""
    f = f"{where}.{key}" if where else key
    if key not in d or (d[key] is None and kind != "num"):
        if required:
            errs.append(f"{f}: missing")
        return default
    v = d[key]
    ok = {
        "int": _is_int(v),
        "num": (_is_int(v) or (isinstance(v, float) and math.isfinite(v))),
        "bool": isinstance(v, bool),
        "str": isinstance(v, str),
        "name": isinstance(v, str) and bool(NAME_RE.match(v.replace("-", "_"))),
        "hex": isinstance(v, str) and bool(HEX_RE.match(v)),
        "any": True,
    }[kind]
    if not ok:
        what = {"num": "a finite number", "int": "an integer", "hex": "a #rrggbb hex colour", "name": "a lowercase id"}
        errs.append(f"{f}: {v!r} is not {what.get(kind, 'a ' + kind)}")
        return default
    if choices is not None and v not in choices:
        errs.append(f"{f}: {v!r} is not one of {', '.join(map(str, choices))}")
        return default
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        errs.append(f"{f}: {v!r} is outside {lo}..{'' if hi is None else hi}")
        return default
    if gt is not None and v <= gt:
        errs.append(f"{f}: {v!r} must be greater than {gt}")
        return default
    return v
