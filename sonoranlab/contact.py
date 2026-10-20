"""Review contact sheet: registered frames with the game's sticker border, validator badges and a foot line.

REVIEW ONLY. The border is a CPU replica of the runtime `PixelStandee.gdshader` and is never baked into exported
sheets (profile `baked_border: forbidden`). Rendering is deterministic: bitmap font only, no PNG metadata.

Sheet layout (all in px; `cw, ch = cell`, `R` rows, `C` columns = longest clip):
    width  = C * (cw * scale + GAP) + GAP
    height = HEADER_H + R * (LABEL_H + ch * scale + BADGE_H + GAP) + GAP + R * (ch + GAP) + FOOTER_H
i.e. header, the scaled grid (label / badge / frame per cell), a true-size 1x strip per row, then a fixed-height footer.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

GAP = 2
HEADER_H = 14
LABEL_H = 12
BADGE_H = 12
LINE_H = 12
FOOTER_LINES = 8
FOOTER_H = FOOTER_LINES * LINE_H + 2 * GAP

DIRECTIONS = ("north", "north-west", "west", "south-west", "south", "south-east", "east", "north-east")
GREEN, RED, GREY, TEXT = (0, 140, 60), (200, 30, 30), (90, 90, 90), (255, 255, 255)
CHECKER = ((196, 196, 196), (168, 168, 168))
PAGE = (48, 48, 48)
_FRAME_FILE = re.compile(r"^(.+)_(\d+)\.png$")


def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    """Chebyshev dilation of a boolean mask by `r` pixels."""
    h, w = mask.shape
    padded = np.pad(mask, r)
    out = np.zeros_like(mask)
    for dy in range(2 * r + 1):
        for dx in range(2 * r + 1):
            out |= padded[dy : dy + h, dx : dx + w]
    return out


def standee_border(
    frame: np.ndarray, keyline: tuple[int, int, int] = (14, 11, 10), border: tuple[int, int, int] = (255, 255, 255)
) -> np.ndarray:
    """Runtime sticker border replica: keyline at Chebyshev distance 1 from opaque (alpha >= 128), border at 2."""
    opaque = frame[..., 3] >= 128
    ring1 = _dilate(opaque, 1) & ~opaque
    ring2 = _dilate(opaque, 2) & ~opaque & ~ring1
    out = frame.copy()
    out[ring1] = (*keyline, 255)
    out[ring2] = (*border, 255)
    return out


def load_clips(frames_dir: Path) -> dict[str, list[np.ndarray]]:
    """Read `<direction>_<i>.png` files as RGBA frames, frames in numeric order, directions in profile order."""
    found: dict[str, dict[int, np.ndarray]] = {}
    for p in sorted(Path(frames_dir).iterdir()):
        if m := _FRAME_FILE.match(p.name):
            found.setdefault(m[1], {})[int(m[2])] = np.asarray(Image.open(p).convert("RGBA"))
    if not found:
        raise ValueError(f"{frames_dir}: no <direction>_<i>.png frames")
    order = [d for d in DIRECTIONS if d in found] + sorted(set(found) - set(DIRECTIONS))
    return {d: [found[d][i] for i in sorted(found[d])] for d in order}


def _render(frame: np.ndarray, scale: int, border: bool, foot_y: int | None) -> Image.Image:
    f = standee_border(frame) if border else frame
    h, w = f.shape[:2]
    yy, xx = np.indices((h, w))
    bg = np.where(((yy + xx) % 2 == 0)[..., None], CHECKER[0], CHECKER[1])
    a = f[..., 3:4].astype(np.float32) / 255
    rgb = np.rint(f[..., :3] * a + bg * (1 - a)).astype(np.uint8)
    big = rgb.repeat(scale, axis=0).repeat(scale, axis=1)
    if foot_y is not None:
        big[(foot_y + 1) * scale - 1] = (255, 0, 0)
    return Image.fromarray(big, "RGB")


def _wrap(text: str, width_px: int, font: ImageFont.ImageFont) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        while font.getlength(word) > width_px and len(word) > 1:  # a word wider than the sheet is split
            k = max(1, int(width_px / font.getlength("M")))
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:k])
            word = word[k:]
        trial = f"{cur} {word}".strip()
        if cur and font.getlength(trial) > width_px:
            lines.append(cur)
            trial = word
        cur = trial
    return [*lines, cur] if cur else lines


def contact_sheet(
    clips: dict[str, list[np.ndarray]],
    report: dict,
    *,
    cell: tuple[int, int],
    foot_y: int,
    scale: int = 8,
    border: bool = True,
    rows: Sequence[str] | None = None,
) -> Image.Image:
    """One row per direction, one column per frame, badges from `report["frames"]`; size per the module docstring."""
    cw, ch = cell
    if scale < 1 or not 0 <= foot_y < ch:
        raise ValueError(f"need scale >= 1 and 0 <= foot_y < {ch}, got scale={scale} foot_y={foot_y}")
    rows = list(clips) if rows is None else list(rows)
    cols = max(len(clips[d]) for d in rows)
    for d in rows:
        for i, f in enumerate(clips[d]):
            if f.shape != (ch, cw, 4):
                raise ValueError(f"{d} frame {i} is {f.shape[1]}x{f.shape[0]}, expected cell {cw}x{ch}")
    recs = {(r["direction"], r["frame"]): r for r in report.get("frames", [])}
    font = ImageFont.load_default_imagefont()
    tw, th = cw * scale, LABEL_H + ch * scale + BADGE_H
    width = cols * (tw + GAP) + GAP
    grid_y = HEADER_H
    strip_y = grid_y + len(rows) * (th + GAP) + GAP
    footer_y = strip_y + len(rows) * (ch + GAP)
    sheet = Image.new("RGB", (width, footer_y + FOOTER_H), PAGE)
    draw = ImageDraw.Draw(sheet)

    fails, warns = report.get("failures", []), report.get("warnings", [])
    ok = bool(report["pass"])
    head = " ".join(
        [
            "PASS" if ok else "FAIL",
            *(str(report[k]) for k in ("character", "animation") if k in report),
            f"failures {len(fails)} warnings {len(warns)}",
        ]
    )
    draw.rectangle((0, 0, width - 1, HEADER_H - 1), fill=GREEN if ok else RED)
    draw.text((GAP + 2, 1), head, fill=TEXT, font=font)

    seen = 0
    for r, d in enumerate(rows):
        for c, f in enumerate(clips[d]):
            rec = recs.get((d, c))
            tile = Image.new("RGB", (tw, th), PAGE)
            td = ImageDraw.Draw(tile)
            td.text((1, 0), f"{d} {c} #{rec['index'] if rec and 'index' in rec else seen}", fill=TEXT, font=font)
            tile.paste(_render(f, scale, border, foot_y), (0, LABEL_H + BADGE_H))
            if rec is None:
                colour, badge = GREY, "--"
            else:
                colour = GREEN if rec["ok"] else RED
                codes = [i.split(":", 1)[0] for i in rec.get("issues", [])]
                badge = " ".join(["OK" if rec["ok"] else "FAIL", *codes])
            td.rectangle((0, LABEL_H, tw - 1, LABEL_H + BADGE_H - 1), fill=colour)  # under its own label
            td.text((2, LABEL_H + 1), badge, fill=TEXT, font=font)
            sheet.paste(tile, (GAP + c * (tw + GAP), grid_y + r * (th + GAP) + GAP))
            sheet.paste(_render(f, 1, border, None), (GAP + c * (cw + GAP), strip_y + r * (ch + GAP)))  # true 1x
            seen += 1

    lines = [f"- {m}" for m in fails] if fails else ["no failures"]
    wrapped = [w for m in lines for w in _wrap(m, width - 2 * GAP, font)]
    if len(wrapped) > FOOTER_LINES:
        wrapped = [*wrapped[: FOOTER_LINES - 1], f"... {len(wrapped) - FOOTER_LINES + 1} more lines, see report.json"]
    for k, line in enumerate(wrapped):
        draw.text((GAP, footer_y + GAP + k * LINE_H), line, fill=TEXT, font=font)
    return sheet
