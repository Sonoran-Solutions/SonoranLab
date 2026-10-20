"""Closed operation schemas and exact pixel predictions at the client trust boundary."""

from __future__ import annotations

import re

import numpy as np

from sonoranlab.repair.authorization import require


def fields(value: dict, allowed: set[str], required: set[str] | None = None) -> None:
    require(isinstance(value, dict) and not set(value) - allowed and (required or set()) <= set(value))


def client_values(value: object) -> None:
    if isinstance(value, dict):
        require(not set(value) & {"path", "mode", "selectionOnly", "sprite", "layer", "frame"}, "forbidden_argument")
        require(value.get("paletteLock", True) is True and value.get("createCel", False) is False, "forbidden_argument")
        for v in value.values():
            client_values(v)
    elif isinstance(value, list):
        for v in value:
            client_values(v)
    elif isinstance(value, str):
        require("../" not in value and "..\\" not in value, "forbidden_argument")


def request_depth(value: object) -> None:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        require(depth <= 32, "invalid_request")
        if isinstance(item, (dict, list)):
            pending.extend((v, depth + 1) for v in (item.values() if isinstance(item, dict) else item))


def integer(value: object) -> int:
    require(type(value) is int)
    return value


def colour(value: object, palette: list[str]) -> np.ndarray:
    require(
        isinstance(value, str) and bool(re.fullmatch("#[0-9a-fA-F]{6}", value)) and value.lower() in palette,
        "invalid_colour",
    )
    return np.array([*bytes.fromhex(value[1:]), 255], np.uint8)


def point(value: dict) -> tuple[int, int]:
    fields(value, {"x", "y"}, {"x", "y"})
    return integer(value["x"]), integer(value["y"])


def rectangle(value: dict, shape: tuple) -> tuple[int, int, int, int]:
    fields(value, {"x", "y", "width", "height"}, {"x", "y", "width", "height"})
    x, y, w, h = (integer(value[k]) for k in ("x", "y", "width", "height"))
    require(x >= 0 and y >= 0 and w > 0 and h > 0 and x + w <= shape[1] and y + h <= shape[0], "mask_violation")
    return x, y, w, h


def covered(mask: np.ndarray, x: int, y: int, w: int = 1, h: int = 1) -> None:
    require(
        x >= 0
        and y >= 0
        and x + w <= mask.shape[1]
        and y + h <= mask.shape[0]
        and bool(mask[y : y + h, x : x + w].all()),
        "mask_violation",
    )


def prepare_op(
    op: str,
    args: dict,
    n: str,
    frames: dict[str, np.ndarray],
    masks: dict[str, np.ndarray],
    palette: list[str],
    origin: tuple[int, int],
    cels: dict[str, dict],
) -> tuple[dict, np.ndarray, tuple[int, int] | None]:
    """Return sanitized upstream args, predicted canvas and a move's inverse delta."""
    client_values(args)
    require(isinstance(args, dict))
    args = dict(args)
    for key in ("paletteLock", "createCel"):
        args.pop(key, None)
    mask, expected = masks[n], frames[n].copy()
    inverse = None
    if op == "draw.pixels":
        fields(args, {"points", "color"}, {"points"})
        points = args["points"]
        require(isinstance(points, list) and 1 <= len(points) <= 65536)
        if "color" in args:
            colour(args["color"], palette)
        for p in points:
            fields(p, {"x", "y", "color"}, {"x", "y"})
            x, y = integer(p["x"]), integer(p["y"])
            covered(mask, x, y)
            expected[y, x] = colour(p.get("color", args.get("color")), palette)
    elif op == "draw.grid":
        fields(args, {"x", "y", "legend", "rows", "transparent"}, {"x", "y", "legend", "rows", "transparent"})
        x, y = integer(args["x"]), integer(args["y"])
        rows, legend = args["rows"], args["legend"]
        require(isinstance(rows, list) and 1 <= len(rows) <= 256 and all(isinstance(r, str) for r in rows))
        require(1 <= len(rows[0]) <= 256 and all(len(r) == len(rows[0]) for r in rows))
        require(isinstance(legend, dict) and all(isinstance(k, str) and len(k) == 1 for k in legend))
        require(args["transparent"] in ("erase", "skip"))
        for v in legend.values():
            if v is not None:
                colour(v, palette)
        covered(mask, x, y, len(rows[0]), len(rows))
        for dy, row in enumerate(rows):
            for dx, key in enumerate(row):
                require(key in legend)
                value = legend[key]
                if value is not None:
                    expected[y + dy, x + dx] = colour(value, palette)
                elif args["transparent"] == "erase":
                    expected[y + dy, x + dx] = 0
    elif op == "draw.clear":
        fields(args, {"region"}, {"region"})
        x, y, w, h = rectangle(args["region"], expected.shape)
        covered(mask, x, y, w, h)
        expected[y : y + h, x : x + w] = 0
    elif op == "draw.blit":
        fields(args, {"from", "to", "fromFrame", "flipHorizontal", "flipVertical", "skipTransparent"}, {"from", "to"})
        source = str(integer(args.get("fromFrame", int(n))))
        require(source in masks, "target_not_authorized")
        x, y, w, h = rectangle(args["from"], expected.shape)
        covered(masks[source], x, y, w, h)
        dx, dy = point(args["to"])
        covered(mask, dx, dy, w, h)
        for key in ("flipHorizontal", "flipVertical", "skipTransparent"):
            require(type(args.get(key, False)) is bool)
        patch = frames[source][y : y + h, x : x + w].copy()
        b = cels[source]
        x0, y0, x1, y1 = b["x"], b["y"], b["x"] + b["width"], b["y"] + b["height"]
        if source == n:
            # Upstream grows the target cel before fetching the source, even for a self-blit.
            x0, y0, x1, y1 = min(x0, dx), min(y0, dy), max(x1, dx + w), max(y1, dy + h)
        yy, xx = np.mgrid[y : y + h, x : x + w]
        valid = (xx >= x0) & (xx < x1) & (yy >= y0) & (yy < y1)
        if args.get("flipHorizontal", False):
            valid = valid[:, ::-1]
            patch = patch[:, ::-1]
        if args.get("flipVertical", False):
            valid = valid[::-1]
            patch = patch[::-1]
        dest = expected[dy : dy + h, dx : dx + w]
        if args.get("skipTransparent", False):
            valid &= patch[..., 3] != 0
        dest[valid] = patch[valid]
        args["fromFrame"], args["fromLayer"] = int(source), "art"
        args["skipTransparent"] = args.get("skipTransparent", False)
    elif op == "cel.move":
        require(set(args) in ({"dx", "dy"}, {"x", "y"}))
        dx = integer(args["dx"]) if "dx" in args else integer(args["x"]) - origin[0]
        dy = integer(args["dy"]) if "dy" in args else integer(args["y"]) - origin[1]
        h, w = expected.shape[:2]
        require(abs(dx) <= w and abs(dy) <= h, "mask_violation")
        if "x" in args:
            require(abs(args["x"]) <= w and abs(args["y"]) <= h, "mask_violation")
        require(bool((frames[n][..., 3] != 0).any()), "empty_cel")
        expected[:] = 0
        for y, x in np.argwhere(frames[n][..., 3] != 0):
            # Both ends must stay inside the authorization, including pixels moved off canvas.
            covered(mask, int(x), int(y))
            covered(mask, int(x + dx), int(y + dy))
            expected[y + dy, x + dx] = frames[n][y, x]
        args, inverse = {"dx": dx, "dy": dy}, (-dx, -dy)
    else:
        require(False, "operation_not_authorized")
    if op.startswith("draw."):
        args = {
            "ops": [{"kind": op.split(".")[1], **args}],
            "paletteLock": True,
            "createCel": False,
            "selectionOnly": False,
        }
    else:
        args = {"op": "move", **args}
    return args, expected, inverse
