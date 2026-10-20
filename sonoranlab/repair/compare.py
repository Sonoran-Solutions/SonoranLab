"""Raw decoded RGBA comparison. Hidden RGB under alpha zero is the sole pixel equivalence exception."""

from __future__ import annotations

import hashlib
import json
import re
import struct
from pathlib import Path

import numpy as np
from PIL import Image

from sonoranlab.artifacts import sha256_file
from sonoranlab.repair import Refused
from sonoranlab.repair.runtime import no_symlinks


def load_rgba(path: Path) -> np.ndarray:
    """Pillow decodes palette/transparency, without ICC/gamma transformations."""
    with Image.open(no_symlinks(path)) as image:
        return np.asarray(image.convert("RGBA")).copy()


def coordinates(mask: np.ndarray) -> list[list[int]]:
    """JSON coordinates are [x, y], ordered by row then column."""
    return np.argwhere(mask)[:, ::-1].tolist()


def pixel_problems(image: np.ndarray, palette: list[str]) -> dict:
    colours = np.array([tuple(bytes.fromhex(c.removeprefix("#"))) for c in palette], dtype=np.uint8)
    allowed = (image[..., None, :3] == colours).all(axis=-1).any(axis=-1)
    return {
        "invalid_alpha": coordinates((image[..., 3] != 0) & (image[..., 3] != 255)),
        "off_palette": coordinates((image[..., 3] == 255) & ~allowed),
    }


def changed_coordinates(before: np.ndarray, after: np.ndarray) -> list[list[int]]:
    """Different canvases are metadata failures; caller must check dimensions before comparing."""
    if before.shape != after.shape:
        raise ValueError("cannot compare coordinates of different canvases")
    visible = (before[..., 3] != 0) | (after[..., 3] != 0)
    return coordinates((before != after).any(axis=-1) & visible)


def png_chunks(path: Path) -> list[dict]:
    """Keep colour-profile/gamma chunks verbatim as hex, as well as their hashes, in export metadata."""
    result = []
    with no_symlinks(path).open("rb") as f:
        if f.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ValueError(f"{path}: not a PNG")
        while header := f.read(8):
            if len(header) != 8:
                raise ValueError(f"{path}: truncated PNG header")
            length, kind = struct.unpack(">I4s", header)
            data = f.read(length)
            if len(data) != length or len(f.read(4)) != 4:
                raise ValueError(f"{path}: truncated PNG chunk")
            if kind in {b"gAMA", b"cHRM", b"iCCP", b"sRGB", b"cICP", b"mDCV", b"cLLI"}:
                result.append(
                    {"type": kind.decode(), "data_hex": data.hex(), "sha256": hashlib.sha256(data).hexdigest()}
                )
            if kind == b"IEND":
                break
    return result


def source_name(entry: dict) -> str:
    if not re.fullmatch(r"[a-z0-9_-]+", entry["direction"]) or entry["kind"] not in {"still", "anim"}:
        raise Refused("invalid frame mapping")
    if not isinstance(entry["index"], int) or entry["index"] < 0:
        raise Refused("invalid frame index")
    return (
        f"still_{entry['direction']}.png" if entry["kind"] == "still" else f"{entry['direction']}_{entry['index']}.png"
    )


def expected_mapping(repair: dict) -> dict:
    entries = [{"kind": "still", "direction": d, "index": 0, "duration_ms": 100} for d in repair["directions"]["order"]]
    entries += [
        {"kind": "anim", "direction": d, "index": i, "duration_ms": repair["duration_ms"]}
        for d in repair["directions"]["generate"]
        for i in range(repair["frames"])
    ]
    return {str(n): entry for n, entry in enumerate(entries, 1)}


def expected_metadata(repair: dict) -> dict:
    mapping = expected_mapping(repair)
    stills = len(repair["directions"]["order"])
    frames = repair["frames"]
    tags = {"still": [1, stills]}
    for i, d in enumerate(repair["directions"]["generate"]):
        tags[f"{repair['anim']}:{d}"] = [stills + i * frames + 1, stills + (i + 1) * frames]
    return {
        "width": repair["cell"][0],
        "height": repair["cell"][1],
        "color_mode": "rgb",
        "palette": ["#00000000", *(c.lower() + "ff" for c in repair["palette"])],
        "layers": ["art"],
        "frame_count": len(mapping),
        "durations_ms": [e["duration_ms"] for e in mapping.values()],
        "tags": tags,
    }


def read_json(path: Path) -> dict:
    path = no_symlinks(path)
    if not path.is_file():
        raise FileNotFoundError(f"{path}: not a regular JSON file")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected JSON object")
    return value


def compare(repair: Path, exported: Path) -> dict:
    """All source cells and document metadata; never reads/writes the parent job or transforms pixels."""
    repair, exported = no_symlinks(repair), no_symlinks(exported)
    doc = read_json(repair / "repair.json")
    mapping = expected_mapping(doc)
    expected = expected_metadata(doc)
    actual = read_json(exported / "meta.json")
    problems = []
    for path in (repair / "mapping.json", exported / "mapping.json"):
        if not path.exists() or read_json(path) != mapping:
            problems.append(f"{path}: mapping differs")
    metadata = {k: {"expected": v, "actual": actual.get(k)} for k, v in expected.items() if actual.get(k) != v}
    if metadata:
        problems.append("document metadata differs")
    want = {f"{n}.png" for n in mapping}
    got = {p.name for p in exported.glob("*.png")}
    if want != got:
        problems.append(f"frame files differ: missing={sorted(want - got)}, extra={sorted(got - want)}")
    frames = {}
    for n, entry in mapping.items():
        src = repair / "source" / source_name(entry)
        if sha256_file(no_symlinks(src)) != doc["source"][src.name]:
            problems.append(f"source/{src.name}: sha256 differs")
        dst = exported / f"{n}.png"
        if not dst.exists():
            continue
        before, after = load_rgba(src), load_rgba(dst)
        checked = pixel_problems(after, doc["palette"])
        same_canvas = before.shape == after.shape
        changed = changed_coordinates(before, after) if same_canvas else []
        frames[n] = {"mapping": entry, "changed": changed, "canvas_matches": same_canvas, **checked}
        if not same_canvas or after.shape[:2] != (expected["height"], expected["width"]):
            problems.append(f"frame {n}: canvas differs")
        if changed or any(checked.values()):
            problems.append(f"frame {n}: pixels differ or violate palette/alpha")
    return {
        "ok": not problems,
        "repair": str(repair),
        "export": str(exported),
        "problems": problems,
        "metadata": metadata,
        "frames": frames,
        "png_chunks": actual.get("png_chunks", {}),
    }


def latest_export(repair: Path) -> Path:
    workspace = no_symlinks(repair / "workspace")
    snapshots = [p for p in workspace.iterdir() if p.name == "export" or re.fullmatch(r"export-\d{4,}", p.name)]
    if not snapshots:
        raise Refused("no export; run repair export first")
    latest = max(snapshots, key=lambda p: 1 if p.name == "export" else int(p.name.split("-")[1]))
    if not no_symlinks(latest / "export.json").is_file():
        raise Refused("latest export is incomplete; run repair export again")
    return latest


def diff(repair: Path) -> dict:
    return compare(repair, latest_export(repair))
