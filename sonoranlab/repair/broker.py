"""Single-writer broker: validate, journal, execute once, verify, and compensate."""

from __future__ import annotations

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
from sonoranlab.repair.operations import client_values, fields, prepare_op, rectangle
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
            require(self.state["state"] in ("prepared", "checkpointed"), "invalid_state")
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
            if self.state["state"] == "checkpointed":
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
            if self.state["state"] == "checkpointed":
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
            self.state["session_state"] = "ACTIVE"
            persist(self.repair / "state.json", self.state)
            self.journal.append(
                event="session_start",
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
        return Snapshot(info, frames)

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

    def cel_origin(self, n: str) -> tuple[int, int]:
        cels = self.client.call("cel", {**self.target(n), "op": "list"})["cels"]
        selected = [c for c in cels if c["frame"] == int(n) and c["layer"] == "art"]
        require(len(selected) == 1, "invalid_editor_state")
        require(type(selected[0]["x"]) is int and type(selected[0]["y"]) is int, "invalid_editor_state")
        return selected[0]["x"], selected[0]["y"]

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
            ok = restored.equals(before) and (origin is None or self.cel_origin(n) == origin)
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
        origin = (0, 0)
        if request["op"] == "cel.move":
            origin = self.cel_origin(n)
        args, expected, inverse = prepare_op(
            request["op"],
            request["args"],
            n,
            before.frames,
            self.masks,
            [c.lower() for c in self.doc["palette"]],
            origin,
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
            actual_origin = self.cel_origin(n)
            geometry_ok = actual_origin == (origin[0] - inverse[0], origin[1] - inverse[1])
            restore_inverse = (origin[0] - actual_origin[0], origin[1] - actual_origin[1])
        changed = before.changes(after)
        same_meta = stable_info(before.info) == stable_info(after.info)
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
        safe_request = {k: v for k, v in request.items() if k != "token"} if isinstance(request, dict) else request
        try:
            self.journal.append(event="request", request=safe_request, before=self.accepted.hashes())
            require(
                isinstance(request, dict)
                and isinstance(request.get("token"), str)
                and hmac.compare_digest(request["token"].encode("utf-8", "surrogatepass"), self.token.encode()),
                "lease_token_invalid",
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
            response = {"ok": False, "error": e.code, "message": str(e)}
        except (ValueError, KeyError, TypeError, IndexError, ToolError) as e:
            response = {"ok": False, "error": "invalid_request", "message": str(e)}
        if self.inflight:
            if self.state["state"] != "DISCONNECTED":
                self.terminal("TAINTED")
            self.inflight = False
        return self.decision(response, ident if isinstance(ident, str) else None)

    def decision(self, response: dict, ident: str | None = None) -> dict:
        self.journal.append(event="decision", op_id=ident, response=response, after=self.accepted.hashes())
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
