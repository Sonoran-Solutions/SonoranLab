"""Single-writer broker: validate, journal, execute once, verify, and compensate."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from sonoranlab.artifacts import sha256_file
from sonoranlab.repair.authorization import (
    Denied,
    canonical,
    frame_number,
    lease,
    open_private,
    persist,
    require,
    safe_tree,
    source_revision,
    validate,
)
from sonoranlab.repair.compare import (
    changed_coordinates,
    expected_mapping,
    expected_metadata,
    load_rgba,
    pixel_problems,
    read_json,
    source_name,
)
from sonoranlab.repair.mcp_client import Disconnected, OutcomeUnknown, ToolError
from sonoranlab.repair.operations import client_values, fields, prepare_op, rectangle, request_depth
from sonoranlab.repair.runtime import AsepriteConverter, no_symlinks


def rgba_hex(c: str) -> str:
    require(isinstance(c, str) and bool(re.fullmatch("#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})", c)), "invalid_editor_state")
    return c.lower() + ("ff" if len(c) == 7 else "")


def metadata(info: dict) -> dict:
    """Projection shared with the batch interchange; keep full live metadata separately."""
    return {
        "width": info["width"],
        "height": info["height"],
        "color_mode": info["colorMode"],
        "palette": [rgba_hex(c) for c in info["palette"]],
        "layers": [v["name"] for v in info["layers"]],
        "frame_count": info["frameCount"],
        "durations_ms": [v["durationMs"] for v in info["frames"]],
        "tags": {v["name"]: [v["from"], v["to"]] for v in info["tags"]},
    }


def stable_info(info: dict) -> dict:
    return {k: v for k, v in info.items() if k not in {"modified", "selection", "activeFrame", "activeLayer"}}


@dataclass
class Snapshot:
    info: dict
    frames: dict[str, np.ndarray]

    def hashes(self) -> dict:
        return {n: hashlib.sha256(a.tobytes()).hexdigest() for n, a in self.frames.items()}

    def packed(self) -> dict:
        return {"info": self.info, "frames": {n: a.tobytes().hex() for n, a in self.frames.items()}}

    @classmethod
    def unpack(cls, value: dict) -> Snapshot:
        h, w = value["info"]["height"], value["info"]["width"]
        return cls(
            value["info"],
            {n: np.frombuffer(bytes.fromhex(v), np.uint8).reshape(h, w, 4).copy() for n, v in value["frames"].items()},
        )

    def changes(self, after: Snapshot) -> dict:
        return {
            n: changed_coordinates(a, after.frames[n])
            if n in after.frames and a.shape == after.frames[n].shape
            else "canvas_changed"
            for n, a in self.frames.items()
        }

    def equals(self, other: Snapshot) -> bool:
        return stable_info(self.info) == stable_info(other.info) and self.hashes() == other.hashes()


class Journal:
    def __init__(self, path: Path, state: dict, state_path: Path) -> None:
        self.state, self.state_path = state, state_path
        self.last = "0" * 64
        self.outcomes: dict[str, dict] = {}
        self.fd = open_private(path, os.O_RDWR | os.O_CREAT | os.O_APPEND)
        try:
            committed = state.get("log_sha256", self.last)
            found = committed == self.last
            with os.fdopen(os.dup(self.fd), "r") as f:
                for line in f:
                    row = json.loads(line)
                    digest = row.pop("sha256")
                    require(
                        row["previous"] == self.last and hashlib.sha256(canonical(row)).hexdigest() == digest,
                        "log_corrupt",
                    )
                    self.last = digest
                    found = found or digest == committed
                    ident = row.get("op_id")
                    if ident:
                        self.outcomes[ident] = row.get("response", {"ok": False, "error": "outcome_unknown"})
            require(found, "log_corrupt")
        except Exception:
            os.close(self.fd)
            raise

    def append(self, **row: object) -> None:
        row.update(previous=self.last, time_ns=time.time_ns())
        digest = hashlib.sha256(canonical(row)).hexdigest()
        data = canonical({**row, "sha256": digest}) + b"\n"
        with os.fdopen(os.dup(self.fd), "ab") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        self.last = digest
        self.state["log_sha256"] = digest
        persist(self.state_path, self.state)
        if row.get("op_id"):
            self.outcomes[row["op_id"]] = row.get("response", {"ok": False, "error": "outcome_unknown"})

    def close(self) -> None:
        os.close(self.fd)


# Upstream reports tostring(layer.blendMode); BlendMode.NORMAL is 3 in the pinned Aseprite 1.3.18.6 (verified in
# batch). ponytail: re-verify this value whenever runtime.ASEPRITE changes.
BLEND_NORMAL = ("normal", "3")


class Broker:
    def __init__(
        self,
        repair: Path,
        *,
        launcher: Callable | None = None,
        converter: AsepriteConverter | None = None,
        ready_timeout: float = 90,
    ) -> None:
        self.repair = no_symlinks(repair)
        self.session = self.repair / "session"
        self.workspace = self.repair / "workspace"
        self.lock = lease(self.repair)
        self.lock.__enter__()
        self.editor = self.journal = None
        self.closed = False
        self.stopping = False
        self.checkpointed = False
        self.inflight = False
        self.converter = converter
        try:
            self.doc = read_json(self.repair / "repair.json")
            require((self.repair / "authorization.json").is_file(), "authorization_missing")
            require((self.repair / "state.json").is_file(), "authorization_hash_mismatch")
            self.state = read_json(self.repair / "state.json")
            restarting = self.state["state"] == "DISCONNECTED"
            require(self.state["state"] in ("prepared", "checkpointed", "DISCONNECTED"), "invalid_state")
            admission = (
                ("checkpointed" if self.state.get("accepted_sha256") else "prepared")
                if restarting
                else self.state["state"]
            )
            self.auth = read_json(self.repair / "authorization.json")
            require(
                sha256_file(self.repair / "authorization.json") == self.state.get("authorization_sha256"),
                "authorization_hash_mismatch",
            )
            self.mapping = read_json(self.repair / "mapping.json")
            require(self.mapping == expected_mapping(self.doc), "mapping_mismatch")
            self.masks = validate(self.auth, self.doc, self.mapping)
            require(self.state.get("iterations", 0) < self.auth["max_iterations"], "iteration_limit")
            self.verify_files()
            self.journal = Journal(self.session / "ops.jsonl", self.state, self.repair / "state.json")
            expected = self.export_document()
            if admission == "checkpointed":
                path = self.session / "accepted.json"
                require(sha256_file(path) == self.state.get("accepted_sha256"), "accepted_hash_mismatch")
                accepted = Snapshot.unpack(read_json(path))
                require(expected.hashes() == accepted.hashes(), "workspace_mismatch")
                require(
                    sha256_file(self.workspace / "doc.aseprite") == self.state.get("document_sha256"),
                    "workspace_mismatch",
                )
            else:
                require(
                    expected.hashes()
                    == {
                        n: self.pixel_hash(load_rgba(self.repair / "source" / source_name(e)))
                        for n, e in self.mapping.items()
                    },
                    "workspace_mismatch",
                )
            if launcher is None:
                from sonoranlab.repair.session import launch_editor

                launcher = launch_editor
            self.editor = launcher(self.repair)
            self.client = self.editor.client
            self.client.initialize()
            deadline = time.monotonic() + ready_timeout
            while time.monotonic() < deadline:
                try:
                    pf = self.client.call(
                        "preflight", {}, timeout=min(self.client.timeout, max(0.01, deadline - time.monotonic()))
                    )
                    if (
                        pf.get("ready")
                        and pf.get("mode") == "live"
                        and pf.get("bridgeConnected")
                        and pf.get("pluginConnected")
                    ):
                        break
                except OutcomeUnknown:
                    pass
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            else:
                raise Denied("editor_not_connected")
            # The extension can attach before Aseprite has opened the argv document: wait for it, same deadline.
            while True:
                listed = self.client.call("sprite_manage", {"op": "list"})["sprites"]
                matches = [s for s in listed if s.get("filename") == "/work/doc.aseprite"]
                if matches or time.monotonic() >= deadline:
                    break
                time.sleep(0.25)
            require(len(matches) == 1 and type(matches[0]["id"]) is int, "wrong_document")
            self.sprite = "#" + str(matches[0]["id"])
            self.accepted = self.snapshot()
            require(
                metadata(self.accepted.info) == expected_metadata(self.doc)
                and self.accepted.hashes() == expected.hashes(),
                "live_document_mismatch",
            )
            self.check_initial_layers(self.accepted.info)
            if admission == "checkpointed":
                # IDs are process-local; other stable properties survive save/reopen.
                old = stable_info(accepted.info)
                new = stable_info(self.accepted.info)
                old.pop("id", None)
                new.pop("id", None)
                require(old == new, "live_document_mismatch")
            self.token = secrets.token_hex(32)
            fd = open_private(self.session / "token", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            with os.fdopen(fd, "w") as f:
                os.fchmod(f.fileno(), 0o600)
                f.write(self.token)
                f.flush()
                os.fsync(f.fileno())
            self.state["state"] = admission
            self.state["session_state"] = "ACTIVE"
            persist(self.repair / "state.json", self.state)
            self.journal.append(
                event="session_start",
                unsaved_edits_lost=restarting,
                authorization_sha256=self.state["authorization_sha256"],
                authorization=self.auth,
                metadata=self.accepted.info,
                before=self.accepted.hashes(),
            )
        except Exception as e:
            self.close()
            if isinstance(e, (Disconnected, OutcomeUnknown, ToolError)):
                raise Denied("editor_not_connected") from e
            raise

    @staticmethod
    def pixel_hash(a: np.ndarray) -> str:
        a[a[..., 3] == 0] = 0
        return hashlib.sha256(a.tobytes()).hexdigest()

    @staticmethod
    def check_initial_layers(info: dict) -> None:
        require(len(info["layers"]) == 1, "live_document_mismatch")
        layer = info["layers"][0]
        require(
            layer["name"] == "art"
            and layer["visible"]
            and layer["editable"]
            and layer["opacity"] == 255
            and layer["blendMode"] in BLEND_NORMAL
            and not layer["isGroup"]
            and not layer["isTilemap"]
            and not layer.get("parent"),
            "live_document_mismatch",
        )
        require(
            all(t["direction"] in ("forward", "0") and not t.get("repeats") for t in info["tags"])
            and not info.get("slices"),
            "live_document_mismatch",
        )

    def verify_files(self) -> None:
        safe_tree(self.workspace)
        safe_tree(self.repair / "source")
        require({p.name for p in (self.repair / "source").iterdir()} == set(self.doc["source"]), "source_hash_mismatch")
        for name, digest in self.doc["source"].items():
            require(Path(name).name == name and ".." not in name, "unsafe_path")
            require(sha256_file(self.repair / "source" / name) == digest, "source_hash_mismatch")
        require(
            sha256_file(self.repair / "authorization.json") == self.state["authorization_sha256"],
            "authorization_hash_mismatch",
        )

    def target(self, n: str = "1") -> dict:
        return {"sprite": self.sprite, "layer": "art", "frame": int(n)}

    def snapshot(self) -> Snapshot:
        for attempt in range(2):
            try:
                return self._snapshot()
            except OutcomeUnknown as e:
                if attempt:
                    raise Disconnected("snapshot read failed twice") from e
        raise AssertionError("unreachable")

    def _snapshot(self) -> Snapshot:
        info = self.client.call("sprite_info", {**self.target(), "includePalette": True, "includeSlices": True})
        require(
            info.get("id") == int(self.sprite[1:]) and info.get("filename") == "/work/doc.aseprite", "wrong_document"
        )
        require(
            info["width"] == self.doc["cell"][0]
            and info["height"] == self.doc["cell"][1]
            and info["frameCount"] == len(self.mapping),
            "invalid_editor_state",
        )
        frames = {}
        for n in self.mapping:
            data = self.client.call("read_pixels", {**self.target(n), "composite": False})
            colours = np.array([list(bytes.fromhex(rgba_hex(c)[1:])) for c in data["colors"]], np.uint8)
            indices = np.array(data["grid"])
            require(
                indices.dtype.kind in "iu"
                and indices.size == info["width"] * info["height"]
                and bool((indices >= 0).all())
                and bool((indices < len(colours)).all()),
                "invalid_editor_state",
            )
            a = colours[indices].reshape(info["height"], info["width"], 4)
            a[a[..., 3] == 0] = 0
            frames[n] = a
        cels = self.client.call("cel", {**self.target(), "op": "list"})["cels"]
        selected = {}
        for n in self.mapping:
            matches = [c for c in cels if c["frame"] == int(n) and c["layer"] == "art"]
            require(len(matches) == 1, "invalid_editor_state")
            cel = matches[0]
            require(all(type(cel[k]) is int for k in ("x", "y", "width", "height")), "invalid_editor_state")
            selected[n] = cel
        return Snapshot({**info, "cels": selected}, frames)

    def export_document(self) -> Snapshot:
        """Explicit batch interchange, only at admission/checkpoint, never disconnect recovery."""
        self.converter = self.converter or AsepriteConverter()
        n = 1
        while True:
            output = self.workspace / ("export" if n == 1 else f"export-{n:04d}")
            try:
                output.mkdir()
                break
            except FileExistsError:
                n += 1
        self.converter.export(self.workspace, output)
        safe_tree(self.workspace)
        meta = read_json(output / "meta.json")
        require(all(meta.get(k) == v for k, v in expected_metadata(self.doc).items()), "workspace_metadata_mismatch")
        require({p.name for p in output.glob("*.png")} == {f"{n}.png" for n in self.mapping}, "workspace_mismatch")
        frames = {}
        for n in self.mapping:
            a = load_rgba(output / f"{n}.png")
            require(
                a.shape == (self.doc["cell"][1], self.doc["cell"][0], 4)
                and not any(pixel_problems(a, self.doc["palette"]).values()),
                "workspace_mismatch",
            )
            a[a[..., 3] == 0] = 0
            frames[n] = a
        persist(output / "mapping.json", self.mapping)
        persist(
            output / "export.json",
            {
                "repair_id": self.doc["repair_id"],
                "doc_sha256": sha256_file(self.workspace / "doc.aseprite"),
                "files": {p.name: sha256_file(p) for p in output.iterdir() if p.is_file()},
            },
        )
        return Snapshot(meta, frames)

    def terminal(self, state: str) -> None:
        self.state["state"] = self.state["session_state"] = state
        persist(self.repair / "state.json", self.state)

    def restore(
        self, before: Snapshot, after: Snapshot | None, n: str, inverse: tuple | None, origin: tuple | None = None
    ) -> bool:
        try:
            if inverse is not None:
                self.client.call("cel", {**self.target(n), "op": "move", "dx": inverse[0], "dy": inverse[1]})
            if after is not None and before.info["frames"] != after.info["frames"]:
                # Internal compensation only: clients cannot invoke structural tools.
                self.client.call(
                    "frame",
                    {
                        **self.target(),
                        "op": "set_duration",
                        "durations": [f["durationMs"] for f in before.info["frames"]],
                    },
                )
            for frame, a in before.frames.items():
                if after is not None and np.array_equal(a, after.frames[frame]) and inverse is None:
                    continue
                h, w = a.shape[:2]
                for y in range(0, h, 256):
                    for x in range(0, w, 256):
                        patch = a[y : y + 256, x : x + 256]
                        # Clear first; grouped opaque points cannot preserve stale transparent pixels.
                        ops = [
                            {
                                "kind": "clear",
                                "region": {"x": x, "y": y, "width": patch.shape[1], "height": patch.shape[0]},
                            }
                        ]
                        for c in np.unique(patch.reshape(-1, 4), axis=0):
                            if c[3] == 0:
                                continue
                            points = [
                                {"x": x + int(px), "y": y + int(py)}
                                for py, px in np.argwhere((patch == c).all(axis=-1))
                            ]
                            ops.append({"kind": "pixels", "color": "#" + bytes(c[:3]).hex(), "points": points})
                        for start in range(0, len(ops), 512):
                            self.client.call(
                                "draw",
                                {
                                    **self.target(frame),
                                    "ops": ops[start : start + 512],
                                    "paletteLock": True,
                                    "createCel": False,
                                    "selectionOnly": False,
                                },
                            )
            restored = self.snapshot()
            ok = restored.equals(before) and (
                origin is None or (restored.info["cels"][n]["x"], restored.info["cels"][n]["y"]) == origin
            )
            self.journal.append(
                event="restore",
                before=before.hashes(),
                after=restored.hashes(),
                changed=before.changes(restored),
                metadata_before=before.info,
                metadata_after=restored.info,
                verified=ok,
            )
            if ok:
                return True
        except Disconnected:
            raise
        except Exception as e:
            self.journal.append(event="restore_failed", error=str(e))
        self.terminal("TAINTED")
        return False

    def mutate(self, request: dict) -> dict:
        fields(
            request,
            {"action", "token", "op_id", "source_revision", "target", "op", "args"},
            {"op_id", "source_revision", "target", "op", "args"},
        )
        client_values({k: v for k, v in request.items() if k != "token"})
        require(request["source_revision"] == source_revision(self.doc), "stale_source_revision")
        fields(request["target"], {"kind", "direction", "index"}, {"kind", "direction"})
        n = frame_number(request["target"], self.mapping)
        require(n in self.masks, "target_not_authorized")
        require(request["op"] in self.auth["operations"], "operation_not_authorized")
        before = self.snapshot()
        if not before.equals(self.accepted):
            self.terminal("TAINTED")
            raise Denied("live_state_drift")
        origin = (before.info["cels"][n]["x"], before.info["cels"][n]["y"])
        args, expected, inverse = prepare_op(
            request["op"],
            request["args"],
            n,
            before.frames,
            self.masks,
            [c.lower() for c in self.doc["palette"]],
            origin,
            before.info["cels"],
        )
        self.journal.append(
            event="dispatch",
            op_id=request["op_id"],
            request={k: v for k, v in request.items() if k != "token"},
            before=before.hashes(),
            metadata_before=before.info,
            authorization_sha256=self.state["authorization_sha256"],
        )
        unknown, tool_error = False, None
        self.inflight = True
        try:
            self.client.call(request["op"].split(".")[0], {**args, **self.target(n)})
        except OutcomeUnknown:
            unknown = True
            self.journal.append(event="outcome_unknown", op_id=request["op_id"], before=before.hashes())
        except Disconnected:
            self.journal.append(event="outcome_unknown", op_id=request["op_id"], before=before.hashes())
            raise
        except ToolError as e:
            tool_error = str(e)
        try:
            after = self.snapshot()
        except Disconnected:
            raise
        except Exception as e:
            self.terminal("TAINTED")
            raise Denied("verification_failed", str(e)) from e
        geometry_ok = True
        restore_inverse = inverse
        if inverse is not None:
            actual_origin = (after.info["cels"][n]["x"], after.info["cels"][n]["y"])
            geometry_ok = actual_origin == (origin[0] - inverse[0], origin[1] - inverse[1]) and all(
                before.info["cels"][n][k] == after.info["cels"][n][k] for k in ("width", "height")
            )
            restore_inverse = (origin[0] - actual_origin[0], origin[1] - actual_origin[1])
        else:
            # Aseprite grows (and does not always trim) a cel when drawing. Every pixel is verified separately; the cel
            # rect may only stay inside bbox(old rect U changed pixels) and the canvas, so it cannot drift on its own.
            cel, old = after.info["cels"][n], before.info["cels"][n]
            h, w = expected.shape[:2]
            diff = np.argwhere((before.frames[n] != after.frames[n]).any(axis=-1))
            lo_x, lo_y, hi_x, hi_y = old["x"], old["y"], old["x"] + old["width"], old["y"] + old["height"]
            if len(diff):
                lo_y, lo_x = min(lo_y, int(diff[:, 0].min())), min(lo_x, int(diff[:, 1].min()))
                hi_y, hi_x = max(hi_y, int(diff[:, 0].max()) + 1), max(hi_x, int(diff[:, 1].max()) + 1)
            lo_x, lo_y, hi_x, hi_y = max(lo_x, 0), max(lo_y, 0), min(hi_x, w), min(hi_y, h)
            geometry_ok = (
                cel["x"] >= lo_x
                and cel["y"] >= lo_y
                and cel["x"] + cel["width"] <= hi_x
                and cel["y"] + cel["height"] <= hi_y
                and (cel == old or len(diff) > 0)
            )
        changed = before.changes(after)
        before_meta, after_meta = stable_info(before.info), stable_info(after.info)
        before_cels, after_cels = before_meta.pop("cels"), after_meta.pop("cels")

        # Drawing can resize/trim the target cel; other cel properties must survive.
        def fixed_cels(cels):
            return {
                f: {k: v for k, v in c.items() if f != n or k not in {"x", "y", "width", "height"}}
                for f, c in cels.items()
            }

        same_meta = before_meta == after_meta and fixed_cels(before_cels) == fixed_cels(after_cels)
        inside = same_meta and all(
            not points or (frame == n and points != "canvas_changed" and all(self.masks[n][y, x] for x, y in points))
            for frame, points in changed.items()
        )
        palette_ok = all(not any(pixel_problems(a, self.doc["palette"]).values()) for a in after.frames.values())
        exact = np.array_equal(expected, after.frames[n])
        unchanged = before.equals(after)
        result = {
            "ok": True,
            "outcome": "unchanged" if unchanged else "applied_within_mask",
            "outcome_unknown": unknown,
            "before": before.hashes(),
            "after": after.hashes(),
            "changed": changed,
        }
        if not inside or not palette_ok or (not (exact and geometry_ok) and not (unknown and unchanged)) or tool_error:
            restored = self.restore(before, after, n, restore_inverse, origin if inverse is not None else None)
            result.update(
                ok=False,
                error="verification_violation" if restored else "tainted",
                restored=restored,
                outcome="violation",
            )
        else:
            self.accepted = after
        self.journal.append(
            event="verification",
            op_id=request["op_id"],
            response=result,
            metadata_before=before.info,
            metadata_after=after.info,
        )
        self.inflight = False
        return result

    def read(self, request: dict) -> dict:
        fields(request, {"action", "token", "target", "tool", "args"}, {"target", "tool"})
        client_values({k: v for k, v in request.items() if k != "token"})
        fields(request["target"], {"kind", "direction", "index"}, {"kind", "direction"})
        n = frame_number(request["target"], self.mapping)
        require(n in self.masks, "target_not_authorized")
        tool, args = request["tool"], request.get("args", {})
        if tool == "sprite_info":
            fields(args, set())
        elif tool == "read_pixels":
            fields(args, {"region"})
            if "region" in args:
                rectangle(args["region"], self.accepted.frames[n].shape)
            args = {**args, "composite": False}
        elif tool == "look":
            fields(args, {"op", "from_target", "to_target"}, {"op"})
            require(args["op"] in ("ascii", "diff", "preview", "filmstrip", "onion"), "operation_not_authorized")
            if args["op"] == "diff":
                args = dict(args)
                for key in ("from", "to"):
                    target = args.pop(key + "_target", request["target"])
                    fields(target, {"kind", "direction", "index"}, {"kind", "direction"})
                    number = frame_number(target, self.mapping)
                    require(number in self.masks, "target_not_authorized")
                    args[key + "Frame"] = int(number)
            else:
                require(set(args) == {"op"})
        else:
            raise Denied("operation_not_authorized")
        return {"ok": True, "result": self.client.call(tool, {**args, **self.target(n)})}

    def checkpoint(self) -> dict:
        before = self.snapshot()
        if not before.equals(self.accepted):
            self.terminal("TAINTED")
            raise Denied("live_state_drift")
        # snapshot verifies the filename immediately before the sole save operation.
        self.client.call("sprite_manage", {**self.target(), "op": "save"})
        try:
            self.verify_files()
            exported = self.export_document()
            require(exported.hashes() == self.accepted.hashes(), "checkpoint_mismatch")
            require(self.snapshot().equals(self.accepted), "checkpoint_mismatch")
        except Disconnected:
            raise
        except Exception:
            self.terminal("TAINTED")
            raise
        path = self.session / "accepted.json"
        persist(path, self.accepted.packed())
        # Charge the cycle durably on its first checkpoint; a crash before stop cannot evade the cap.
        if not self.checkpointed:
            self.state["iterations"] = self.state.get("iterations", 0) + 1
        self.state.update(
            state="checkpointed",
            accepted_sha256=sha256_file(path),
            document_sha256=sha256_file(self.workspace / "doc.aseprite"),
        )
        persist(self.repair / "state.json", self.state)
        self.checkpointed = True
        return {"ok": True, "state": "checkpointed", "frames": self.accepted.hashes()}

    def handle(self, request: dict) -> dict:
        ident = None
        try:
            require(
                isinstance(request, dict)
                and isinstance(request.get("token"), str)
                and hmac.compare_digest(request["token"].encode("utf-8", "surrogatepass"), self.token.encode()),
                "lease_token_invalid",
            )
            request_depth(request)
            self.journal.append(
                event="request",
                request={k: v for k, v in request.items() if k != "token"},
                before=self.accepted.hashes(),
            )
            action = request.get("action")
            if action == "status":
                fields(request, {"action", "token"})
                response = {
                    "ok": True,
                    "state": self.state["state"],
                    "session_state": self.state["session_state"],
                    "iterations": self.state.get("iterations", 0),
                }
                return self.decision(response)
            if action == "stop":
                fields(request, {"action", "token", "checkpoint"})
                require(type(request.get("checkpoint", False)) is bool)
                if request.get("checkpoint"):
                    require(self.state["state"] not in ("TAINTED", "DISCONNECTED"), "invalid_state")
                    self.verify_files()
                    self.checkpoint()
                self.stopping = True
                return self.decision({"ok": True, "state": self.state["state"], "stopped": True})
            require(
                not self.stopping and self.state["state"] not in ("TAINTED", "DISCONNECTED"),
                self.state["state"].lower(),
            )
            require(read_json(self.repair / "state.json")["state"] in ("prepared", "checkpointed"), "invalid_state")
            self.verify_files()
            if action == "op":
                ident = request.get("op_id")
                require(isinstance(ident, str) and bool(re.fullmatch("[A-Za-z0-9_-]{1,128}", ident)), "invalid_op_id")
                if ident in self.journal.outcomes:
                    return self.decision(self.journal.outcomes[ident])
                response = self.mutate(request)
            elif action == "read":
                response = self.read(request)
            elif action == "checkpoint":
                fields(request, {"action", "token"})
                response = self.checkpoint()
            else:
                raise Denied("operation_not_authorized")
        except Disconnected:
            self.terminal("DISCONNECTED")
            response = {"ok": False, "error": "disconnected", "state": "DISCONNECTED"}
        except OutcomeUnknown:
            self.terminal("TAINTED")
            response = {"ok": False, "error": "outcome_unknown", "state": "TAINTED"}
        except Denied as e:
            if e.code == "lease_token_invalid":
                action = request.get("action") if isinstance(request, dict) else None
                self.journal.append(
                    action=action if action in ("op", "read", "status", "checkpoint", "stop") else None, error=e.code
                )
                return {"ok": False, "error": e.code, "message": str(e)}
            response = {"ok": False, "error": e.code, "message": str(e)}
        except (ValueError, KeyError, TypeError, IndexError, RecursionError, ToolError) as e:
            response = {"ok": False, "error": "invalid_request", "message": str(e)}
        if self.inflight:
            if self.state["state"] != "DISCONNECTED":
                self.terminal("TAINTED")
            self.inflight = False
        return self.decision(
            response,
            ident if isinstance(ident, str) else None,
            read=isinstance(request, dict) and request.get("action") == "read",
        )

    def decision(self, response: dict, ident: str | None = None, *, read: bool = False) -> dict:
        def summarized(value):
            if isinstance(value, dict):
                result = {}
                for k, v in value.items():
                    if (
                        k in {"grid", "colors", "gridRows"}
                        or (k == "text" and value.get("op") in ("ascii", "diff"))
                        or (k == "data" and value.get("type") == "image")
                    ):
                        data = (
                            base64.b64decode(v) if k == "data" else (v.encode() if isinstance(v, str) else canonical(v))
                        )
                        result[k] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                    else:
                        result[k] = summarized(v)
                return result
            if isinstance(value, list):
                return [summarized(v) for v in value]
            return value

        self.journal.append(
            event="decision",
            op_id=ident,
            response=summarized(response) if read else response,
            after=self.accepted.hashes(),
        )
        return response

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.editor:
                self.editor.close()
            if self.stopping:
                self.state["session_state"] = "STOPPED"
                persist(self.repair / "state.json", self.state)
            if self.journal:
                self.journal.append(event="session_end", stopped=self.stopping)
                self.journal.close()
        finally:
            self.lock.__exit__(None, None, None)
