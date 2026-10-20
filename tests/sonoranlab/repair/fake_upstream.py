"""Independent stdio MCP document model; fault switches belong to the launcher, never broker requests."""

from __future__ import annotations

import base64
import io
import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image


def main() -> None:
    repair, fault = Path(sys.argv[1]), sys.argv[2]
    workspace = repair / "workspace"
    doc = json.loads((workspace / "doc.aseprite").read_text())
    meta = doc["meta"]
    frames = {
        str(i): np.array(Image.open(workspace / "fake-frames" / f"{i}.png").convert("RGBA"))
        for i in range(1, meta["frame_count"] + 1)
    }
    origins = doc.get("origins", {n: [0, 0] for n in frames})
    info = {
        "id": 1,
        "filename": "/work/doc.aseprite",
        "name": "doc.aseprite",
        "width": meta["width"],
        "height": meta["height"],
        "colorMode": meta["color_mode"],
        "frameCount": meta["frame_count"],
        "palette": meta["palette"],
        "layers": [
            {
                "name": "art",
                "index": 0,
                "visible": True,
                "editable": True,
                "opacity": 255,
                "blendMode": "3",
                "isGroup": False,
                "isTilemap": False,
                "cels": list(range(1, meta["frame_count"] + 1)),
            }
        ],
        "frames": [{"number": i, "durationMs": d} for i, d in enumerate(meta["durations_ms"], 1)],
        "tags": [
            {"name": k, "from": v[0], "to": v[1], "direction": "0", "repeats": 0} for k, v in meta["tags"].items()
        ],
        "modified": False,
        "slices": [],
        "selection": None,
        "activeLayer": "art",
        "activeFrame": 1,
    }
    if fault == "live_mismatch":
        frames["1"][0, 0] = [1, 2, 3, 255]
    if fault == "wrong_document":
        info["filename"] = "/work/other.aseprite"
    mutated = False
    snapshots = 0
    dropped = 0
    origin_fault = False
    unrelated = {"filename": "/work/unrelated.aseprite", "pixels": "#abcdef"}

    def rgba(value):
        return list(bytes.fromhex(value[1:])) + ([255] if len(value) == 7 else [])

    def call(name, a):
        nonlocal mutated, snapshots, dropped, origin_fault
        if name == "preflight":
            assert a == {}
            return {"ready": fault != "not_ready", "mode": "live", "bridgeConnected": True, "pluginConnected": True}
        if name == "sprite_manage" and a["op"] == "list":
            return {
                "sprites": [
                    {"id": 2, "filename": unrelated["filename"], "active": True},
                    {"id": 1, "filename": info["filename"], "active": False},
                ]
            }
        assert a["sprite"] == "#1" and a["layer"] == "art" and str(a["frame"]) in frames, a
        n = str(a["frame"])
        if name == "sprite_info":
            snapshots += 1
            if (snapshots > 1 and fault in ("snapshot_drop", "snapshot_drop_twice")) or (
                mutated and fault == "snapshot_after_drop_twice"
            ):
                limit = 1 if fault == "snapshot_drop" else 2
                if dropped < limit:
                    dropped += 1
                    return {"_drop": True}
            return info
        if name == "read_pixels":
            assert a["composite"] is False
            pixels = frames[n].copy()
            pixels[pixels[..., 3] == 0] = 0
            if "region" in a:
                r = a["region"]
                pixels = pixels[r["y"] : r["y"] + r["height"], r["x"] : r["x"] + r["width"]]
            colours, grid = np.unique(pixels.reshape(-1, 4), axis=0, return_inverse=True)
            return {"colors": ["#" + bytes(c).hex() for c in colours], "grid": grid.tolist()}
        if name == "look" and a["op"] in ("preview", "filmstrip", "onion"):
            out = io.BytesIO()
            Image.fromarray(frames[n]).save(out, format="PNG")
            return {
                "content": [
                    {"type": "image", "mimeType": "image/png", "data": base64.b64encode(out.getvalue()).decode()}
                ]
            }
        if name == "look":
            return {"op": a["op"], "text": "fake view", "gridRows": ["..x", "..."]}
        if name == "cel" and a["op"] == "list":
            bounds = {}
            for f, pixels in frames.items():
                opaque = np.argwhere(pixels[..., 3] != 0)
                if len(opaque):
                    y0, x0 = opaque.min(axis=0)
                    y1, x1 = opaque.max(axis=0) + 1
                    origins[f] = [int(x0), int(y0)]
                    bounds[f] = [int(x1 - x0), int(y1 - y0)]
                else:
                    bounds[f] = [1, 1]
            result = {
                "cels": [
                    {
                        "layer": "art",
                        "frame": int(f),
                        "x": origins[f][0],
                        "y": origins[f][1],
                        "width": bounds[f][0],
                        "height": bounds[f][1],
                    }
                    for f in frames
                ]
            }
            if origin_fault:
                result["cels"][0]["x"] += 1
                origin_fault = False
            return result
        if name == "sprite_manage" and a["op"] == "save":
            assert info["filename"] == "/work/doc.aseprite"
            if fault == "save_mismatch":
                frames["1"][0, 0] = rgba(meta["palette"][1])
            for num, pixels in frames.items():
                Image.fromarray(pixels).save(workspace / "fake-frames" / f"{num}.png")
            doc["origins"] = origins
            meta["durations_ms"] = [f["durationMs"] for f in info["frames"]]
            (workspace / "doc.aseprite").write_text(json.dumps(doc))
            return {"op": "save"}
        if name == "frame":
            assert mutated and a["op"] == "set_duration"
            for f, duration in zip(info["frames"], a["durations"], strict=True):
                f["durationMs"] = duration
            return {"applied": 1}
        assert name in ("draw", "cel")
        first = not mutated
        mutated = True
        if first and fault == "eof":
            sys.exit(0)
        if first and fault == "not_connected":
            return {"_error": "not_connected", "doNotFallBackToDisk": True}
        if first and fault == "drop_unchanged":
            return {"_drop": True}
        if not first and fault == "restore_failure":
            return {"_error": "restore failed"}
        if name == "cel":
            assert a["op"] == "move"
            dx, dy = a["dx"], a["dy"]
            out = np.zeros_like(frames[n])
            for y, x in np.argwhere(frames[n][..., 3] != 0):
                if 0 <= y + dy < out.shape[0] and 0 <= x + dx < out.shape[1]:
                    out[y + dy, x + dx] = frames[n][y, x]
            frames[n] = out
            origins[n][0] += dx
            origins[n][1] += dy
        else:
            assert a["paletteLock"] is True and a["createCel"] is False and a["selectionOnly"] is False
            assert 1 <= len(a["ops"]) <= 512
            pixels = frames[n]
            for op in a["ops"]:
                kind = op["kind"]
                if kind == "pixels":
                    for p in op["points"]:
                        pixels[p["y"], p["x"]] = rgba(p.get("color", op.get("color")))
                elif kind == "clear":
                    r = op["region"]
                    pixels[r["y"] : r["y"] + r["height"], r["x"] : r["x"] + r["width"]] = 0
                elif kind == "grid":
                    for y, row in enumerate(op["rows"]):
                        for x, char in enumerate(row):
                            c = op["legend"][char]
                            if c is not None or op["transparent"] == "erase":
                                pixels[op["y"] + y, op["x"] + x] = rgba(c) if c else 0
                elif kind == "blit":
                    r, to = op["from"], op["to"]
                    source = frames[str(op["fromFrame"])]
                    valid = np.zeros(source.shape[:2], dtype=bool)
                    opaque = np.argwhere(source[..., 3] != 0)
                    if len(opaque):
                        y0, x0 = opaque.min(axis=0)
                        y1, x1 = opaque.max(axis=0) + 1
                    else:
                        x0, y0 = origins[str(op["fromFrame"])]
                        x1, y1 = x0 + 1, y0 + 1
                    if str(op["fromFrame"]) == n:
                        # draw grows its target before looking up a same-frame source cel.
                        x0, y0 = min(x0, to["x"]), min(y0, to["y"])
                        x1, y1 = max(x1, to["x"] + r["width"]), max(y1, to["y"] + r["height"])
                    valid[y0:y1, x0:x1] = True
                    valid = valid[r["y"] : r["y"] + r["height"], r["x"] : r["x"] + r["width"]]
                    patch = source[r["y"] : r["y"] + r["height"], r["x"] : r["x"] + r["width"]].copy()
                    if op.get("flipHorizontal"):
                        valid = valid[:, ::-1]
                        patch = patch[:, ::-1]
                    if op.get("flipVertical"):
                        valid = valid[::-1]
                        patch = patch[::-1]
                    dest = pixels[to["y"] : to["y"] + r["height"], to["x"] : to["x"] + r["width"]]
                    if op.get("skipTransparent", True):
                        valid &= patch[..., 3] != 0
                    dest[valid] = patch[valid]
                else:
                    raise AssertionError(kind)
        if first:
            if fault == "origin":
                origin_fault = True
            if fault in ("outside", "drop_violation", "restore_failure"):
                frames[n][15, 15] = rgba(meta["palette"][1])
            elif fault == "frame":
                frames["2"][0, 0] = rgba(meta["palette"][1])
            elif fault == "duration":
                info["frames"][0]["durationMs"] += 1
            elif fault == "snap":
                frames[n][0, 0] = rgba(meta["palette"][2])
            elif fault == "delay":
                time.sleep(0.15)
            elif fault == "eof_after":
                sys.exit(0)
            elif fault == "malformed":
                frames[n] = np.zeros((1, 1, 4), np.uint8)
            if fault in ("drop", "drop_violation"):
                return {"_drop": True}
        return {"applied": 1}

    for line in sys.stdin:
        message = json.loads(line)
        with (repair / "session/fake-calls.jsonl").open("a") as f:
            f.write(json.dumps(message) + "\n")
        if "id" not in message:
            continue
        if message["method"] == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "serverInfo": {"name": "fake", "version": "1"},
            }
        else:
            result = call(message["params"]["name"], message["params"]["arguments"])
            (repair / "session/fake-unrelated.json").write_text(json.dumps(unrelated))
            if result.get("_drop"):
                continue
            result = {"structuredContent": result, "isError": "_error" in result}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main()
