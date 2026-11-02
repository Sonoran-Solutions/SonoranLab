"""`view_artifact` (DYL-130): bounded, allowlisted, read-only PNG viewing so an agent sees pixels, not hashes.

Refs only (never paths): `<job id>:<relative>` for native jobs (compose/delivery jobs too), sync-tool outputs and
imported artifacts, `refs-<id>:still_<direction>.png` for registered reference stills. A file outside STATE is
admitted only when it is a hash-bound file of an imported artifact. The image travels only as MCP ImageContent: the
handler hands its bytes to `capture()` (the MCP server installs one per call); the CLI twin gets the structured part.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from sonoranlab import registry as records
from sonoranlab.api import paths
from sonoranlab.api.registry import Context, Tool, ToolError
from sonoranlab.artifacts import sha256_file
from sonoranlab.jobs import store

MAX_OUTPUT = 4 * 1024 * 1024  # delivered PNG bytes
MAX_SOURCE = 16 * 1024 * 1024  # bytes read from disk
MAX_PIXELS = 64 * 1024 * 1024  # decoded source pixels (decompression-bomb bound)
PNG = b"\x89PNG\r\n\x1a\n"
OUTPUT_TOOLS = ("validate", "contact_sheet", "compare")
_REFS = re.compile(r"refs-[0-9a-f]{12}")
_SINK: ContextVar[list[bytes] | None] = ContextVar("view_artifact_images", default=None)


@contextmanager
def capture():
    """Collect images emitted by handlers in this context (the MCP server); without it nothing is collected."""
    images: list[bytes] = []
    token = _SINK.set(images)
    try:
        yield images
    finally:
        _SINK.reset(token)


def _root(identifier: str, ctx: Context) -> tuple[Path, dict | None]:
    """(artifact root, import entry or None). Every root is confined; imports must still match their hashes."""
    state = paths.state_root()
    if _REFS.fullmatch(identifier):
        hits = sorted((state / "registry").glob(f"*/*/refs/{identifier}"))
        if len(hits) != 1:
            raise ToolError("not_found", "Unknown or ambiguous reference set", field="ref")
        return records._state(hits[0], ctx), None
    if native := records._native_job(identifier):
        return store.output(native), None
    if re.fullmatch(store.JOB_ID, identifier):
        hits = [state / "outputs" / tool / identifier for tool in OUTPUT_TOOLS]
        if hits := [p for p in hits if p.is_dir() and not p.is_symlink()]:
            return records._state(hits[0], ctx), None
    entry = records._find_job(identifier, ctx)
    original = records._directory(entry["job_dir"], ctx)
    for name, digest in entry["hashes"].items():
        if sha256_file(records._job_file(original, name)) != digest:
            raise ToolError("refused", "Imported artifact changed since registration", field="ref")
    return records._review_directory(entry, ctx), entry


def resolve(ref: str, ctx: Context) -> Path:
    identifier, separator, relative = ref.partition(":")
    if not separator or not relative:
        raise ToolError("invalid_params", "Expected <id>:<relative PNG path>", field="ref")
    rel = paths._path(relative)
    if rel.is_absolute():
        raise ToolError("path_refused", "Artifact path must be relative", field="ref")
    root, entry = _root(identifier, ctx)
    lexical = root / rel
    target = paths._confine(lexical, (root,))
    records._protected(target, ctx)
    if any(p.is_symlink() for p in (lexical, *lexical.parents) if p.is_relative_to(root)):
        raise ToolError("path_refused", "Symlinked artifacts are refused", field="ref")
    if target.suffix.lower() != ".png":
        raise ToolError("refused", "Only PNG artifacts can be viewed", field="ref")
    if not target.is_relative_to(paths.state_root()):
        hashed = entry and target.relative_to(Path(entry["job_dir"]).resolve()).as_posix() in entry["hashes"]
        if not hashed:
            raise ToolError("path_refused", "Outside STATE only hash-bound imported files can be viewed", field="ref")
    return target


def _read(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ToolError("path_refused", "Artifact is not a regular file", field="ref")
        if info.st_size > MAX_SOURCE:
            raise ToolError("refused", f"Artifact exceeds {MAX_SOURCE} bytes", field="ref", hint="use contact_sheet")
        data = stream.read(MAX_SOURCE + 1)
    if len(data) > MAX_SOURCE or not data.startswith(PNG):
        raise ToolError("refused", "Artifact is not a bounded PNG file", field="ref")
    return data


def _factor(width: int, height: int, max_edge: int) -> int | None:
    """Smallest exact integer downscale that fits, or None (nearest sampling of a non-integer factor drops pixels)."""
    for k in range(-(-max(width, height) // max_edge), max(width, height) + 1):
        if width % k == 0 and height % k == 0:
            return k
    return None


def view_artifact(ref: str, zoom: int = 1, max_edge: int = 2048, *, ctx: Context) -> dict:
    from PIL import Image

    path = resolve(ref, ctx)
    data = _read(path)
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG":
                raise ToolError("refused", "Artifact is not a PNG", field="ref")
            width, height = image.size
            if width * height > MAX_PIXELS:
                raise ToolError("refused", "Artifact exceeds the decoded pixel bound", field="ref")
            downscale = 1
            if zoom > 1 and max(width, height) * zoom > max_edge:
                raise ToolError(
                    "refused", f"{width}x{height} at zoom {zoom} exceeds max_edge {max_edge}", field="zoom",
                    hint=f"use zoom <= {max(1, max_edge // max(width, height))}",
                )  # fmt: skip
            if zoom == 1 and max(width, height) > max_edge:
                downscale = _factor(width, height, max_edge)
                if downscale is None:
                    raise ToolError(
                        "refused", f"{width}x{height} exceeds max_edge {max_edge} with no exact integer downscale",
                        field="max_edge", hint="render a smaller contact_sheet (zoom) and view that",
                    )  # fmt: skip
            if zoom == 1 and downscale == 1:
                out, size = data, (width, height)
            else:
                size = (width * zoom // downscale, height * zoom // downscale)
                buffer = io.BytesIO()
                image.resize(size, Image.Resampling.NEAREST).save(buffer, "PNG")
                out = buffer.getvalue()
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ToolError("refused", f"Unreadable PNG ({type(exc).__name__})", field="ref") from None
    if len(out) > MAX_OUTPUT:
        raise ToolError("refused", f"Image exceeds {MAX_OUTPUT} bytes", field="zoom", hint="lower zoom or max_edge")
    if (sink := _SINK.get()) is not None:
        sink.append(out)
    return {
        "ref": ref,
        "sha256": hashlib.sha256(out).hexdigest(),
        "size": len(out),
        "dimensions": list(size),
        "zoom": zoom,
        "downscale": downscale,
        "mime_type": "image/png",
        "source": {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "dimensions": [width, height]},
        "delivery": "MCP ImageContent only; the CLI returns this metadata and writes nothing",
    }


TOOLS = (
    Tool(
        "view_artifact",
        "View one PNG artifact in STATE (contact sheets, stills, sheets, renders) as a bounded image; nearest only.",
        {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "minLength": 3, "maxLength": 4096},
                "zoom": {"type": "integer", "minimum": 1, "maximum": 8, "default": 1},
                "max_edge": {"type": "integer", "minimum": 1, "maximum": 2048, "default": 2048},
            },
            "required": ["ref"],
            "additionalProperties": False,
        },
        True,
        "sync",
        lambda p, c: view_artifact(**p, ctx=c),
    ),
)
