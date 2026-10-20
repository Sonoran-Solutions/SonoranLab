"""The containment contract against an independent stdio editor, without a GUI/network."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from test_interchange import fixture, hashes, prepare  # noqa: F401

from sonoranlab.cli import main
from sonoranlab.repair import runtime
from sonoranlab.repair.authorization import Denied, authorize, source_revision
from sonoranlab.repair.broker import Broker
from sonoranlab.repair.compare import read_json
from sonoranlab.repair.mcp_client import MCPClient
from sonoranlab.repair.session import abort, request, serve
from sonoranlab.repair.workspace import FakeConverter


def authorization(repair):
    doc = read_json(repair / "repair.json")
    return {
        "schema": "sonoranlab.repair-authorization/1",
        "repair_id": doc["repair_id"],
        "source_revision": source_revision(doc),
        "parent": {k + "_sha256": doc["parent_hashes"].get(k + ".json") for k in ("report", "manifest", "review")},
        "operations": ["draw.pixels", "draw.grid", "draw.clear", "draw.blit", "cel.move"],
        "targets": [
            {
                "kind": "still",
                "direction": "north",
                "mask": {"rects": [[0, 0, 8, 8]], "pixels": [[9, 9]]},
                "derive_mirror": False,
            }
        ],
        "max_iterations": 2,
        "intent": "synthetic repair",
        "authorized_by": "test",
        "authorized_at": "2026-10-20T12:00:00+00:00",
    }


def launcher(fault="none", timeout=0.5):
    def launch(repair):
        p = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("fake_upstream.py")), str(repair), fault],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        client = MCPClient(p, timeout=timeout)
        return SimpleNamespace(client=client, close=client.close)

    return launch


@pytest.fixture
def repair(fixture):  # noqa: F811
    path = prepare(fixture)
    authorize(path, authorization(path))
    return path


def start(repair, fault="none", timeout=2.0):
    return Broker(repair, launcher=launcher(fault, timeout), converter=FakeConverter(), ready_timeout=0.5)


def op(broker, ident="one", **updates):
    return {
        "action": "op",
        "token": broker.token,
        "op_id": ident,
        "source_revision": broker.auth["source_revision"],
        "target": {"kind": "still", "direction": "north"},
        "op": "draw.pixels",
        "args": {"color": broker.doc["palette"][0], "points": [{"x": 0, "y": 0}]},
        **updates,
    }


def calls(repair, name=None):
    rows = [json.loads(line) for line in (repair / "session/fake-calls.jsonl").read_text().splitlines()]
    return [r["params"] for r in rows if r["method"] == "tools/call" and (name is None or r["params"]["name"] == name)]


def test_authorization_written_once_and_immutable(repair):
    assert (repair / "authorization.json").stat().st_mode & 0o777 == 0o444
    with pytest.raises(FileExistsError):
        authorize(repair, authorization(repair))


@pytest.mark.parametrize(
    "change", ["revision", "parent", "mirror", "empty", "bounds", "target", "time", "iterations", "extra", "bool"]
)
def test_bad_authorization(fixture, change):  # noqa: F811
    repair = prepare(fixture)
    spec = authorization(repair)
    if change == "revision":
        spec["source_revision"] = "old"
    elif change == "parent":
        spec["parent"]["report_sha256"] = "wrong"
    elif change == "mirror":
        spec["targets"][0]["derive_mirror"] = True
    elif change == "empty":
        spec["targets"][0]["mask"] = {}
    elif change == "bounds":
        spec["targets"][0]["mask"]["rects"] = [[15, 15, 2, 2]]
    elif change == "target":
        spec["targets"][0]["direction"] = "../north"
    elif change == "time":
        spec["authorized_at"] = "2026-10-20"
    elif change == "iterations":
        spec["max_iterations"] = 3
    elif change == "extra":
        spec["extra"] = True
    elif change == "bool":
        spec["targets"][0]["mask"]["pixels"] = [[True, 0]]
    with pytest.raises(Denied):
        authorize(repair, spec)
    assert not (repair / "authorization.json").exists()


def test_lease_tokens_replay_checkpoint_iterations_and_parent_unchanged(repair, fixture):  # noqa: F811
    parent = hashes(fixture[0])
    source = hashes(repair / "source")
    b = start(repair)
    old = b.token
    try:
        with pytest.raises(Denied, match="lease_held"):
            start(repair)
        for token in ("", "wrong", "🔒", None):
            assert b.handle(op(b, token=token))["error"] == "lease_token_invalid"
        result = b.handle(op(b))
        assert result["ok"] and result["changed"]["1"] == [[0, 0]]
        assert b.handle(op(b)) == result
        assert len(calls(repair, "draw")) == 1
        assert json.loads((repair / "session/fake-unrelated.json").read_text()) == {
            "filename": "/work/unrelated.aseprite",
            "pixels": "#abcdef",
        }
        assert b.handle({"action": "stop", "token": b.token, "checkpoint": True})["ok"]
    finally:
        b.close()
    b = start(repair)
    try:
        assert b.handle(op(b, token=old))["error"] == "lease_token_invalid"
        assert b.handle(op(b)) == result  # replay survives process/session restart
        assert len(calls(repair, "draw")) == 1
        assert b.handle({"action": "stop", "token": b.token, "checkpoint": True})["ok"]
    finally:
        b.close()
    with pytest.raises(Denied, match="iteration_limit"):
        start(repair)
    abort(repair)
    assert hashes(fixture[0]) == parent and hashes(repair / "source") == source


@pytest.mark.parametrize(
    "tamper,code",
    [
        ("auth", "authorization_hash_mismatch"),
        ("source", "source_hash_mismatch"),
        ("mapping", "mapping_mismatch"),
        ("workspace", "workspace_mismatch"),
        ("symlink", "symlink"),
        ("hardlink", "unsafe_path"),
        ("revision", "stale_source_revision"),
    ],
)
def test_admission_tampering(repair, tamper, code):
    if tamper == "auth":
        p = repair / "authorization.json"
        p.chmod(0o644)
        p.write_text(p.read_text() + " ")
    elif tamper == "source":
        p = repair / "source/still_north.png"
        p.chmod(0o644)
        p.write_bytes(p.read_bytes() + b"!")
    elif tamper == "mapping":
        p = repair / "mapping.json"
        p.write_text("{}")
    elif tamper == "workspace":
        from PIL import Image

        p = repair / "workspace/fake-frames/1.png"
        a = np.array(Image.open(p))
        a[0, 0] = [20, 30, 40, 255]
        Image.fromarray(a).save(p)
    elif tamper == "symlink":
        (repair / "workspace/escape").symlink_to(repair / "source")
    elif tamper == "hardlink":
        os.link(repair / "source/still_north.png", repair / "workspace/escape")
    elif tamper == "revision":
        from sonoranlab.artifacts import sha256_file

        spec = read_json(repair / "authorization.json")
        spec["source_revision"] = "old"
        (repair / "authorization.json").chmod(0o644)
        (repair / "authorization.json").write_text(json.dumps(spec))
        state = read_json(repair / "state.json")
        state["authorization_sha256"] = sha256_file(repair / "authorization.json")
        (repair / "state.json").write_text(json.dumps(state))
    with pytest.raises(ValueError, match=code):
        start(repair)
    assert not (repair / "session/fake-calls.jsonl").exists()


@pytest.mark.parametrize(
    "fault,code",
    [
        ("wrong_document", "wrong_document"),
        ("live_mismatch", "live_document_mismatch"),
        ("not_ready", "editor_not_connected"),
    ],
)
def test_live_admission(repair, fault, code):
    with pytest.raises(Denied, match=code):
        start(repair, fault)
    assert not calls(repair, "draw")
    b = start(repair)
    b.close()  # failed startup released lease


@pytest.mark.parametrize(
    "kind",
    [
        "outside",
        "grid_erase",
        "blit_source",
        "blit_frame",
        "move",
        "palette",
        "alpha",
        "path",
        "traversal",
        "frame",
        "sprite",
        "mode",
        "selection",
        "create",
        "unlock",
        "revision",
        "target",
        "op",
        "bool",
    ],
)
def test_prevalidation_never_mutates(repair, kind):
    b = start(repair)
    try:
        req = op(b)
        if kind == "outside":
            req["args"]["points"][0]["x"] = 15
        elif kind == "grid_erase":
            req.update(
                op="draw.grid", args={"x": 7, "y": 0, "rows": [".."], "legend": {".": None}, "transparent": "erase"}
            )
        elif kind in ("blit_source", "blit_frame"):
            req.update(
                op="draw.blit",
                args={
                    "from": {"x": 15 if kind == "blit_source" else 0, "y": 0, "width": 1, "height": 1},
                    "to": {"x": 0, "y": 0},
                    "fromFrame": 2 if kind == "blit_frame" else 1,
                },
            )
        elif kind == "move":
            req.update(op="cel.move", args={"dx": 15, "dy": 0})
        elif kind in ("palette", "alpha"):
            req["args"]["color"] = "#010203" if kind == "palette" else "#141e2880"
        elif kind in ("path", "traversal", "frame", "sprite", "mode", "selection", "create", "unlock"):
            key, value = {
                "path": ("path", "/tmp/escape"),
                "traversal": ("color", "../escape"),
                "frame": ("frame", 2),
                "sprite": ("sprite", "#2"),
                "mode": ("mode", "headless"),
                "selection": ("selectionOnly", False),
                "create": ("createCel", True),
                "unlock": ("paletteLock", False),
            }[kind]
            req["args"][key] = value
        elif kind == "revision":
            req["source_revision"] = "stale"
        elif kind == "target":
            req["target"]["direction"] = "south"
        elif kind == "op":
            req["op"] = "run_lua"
        elif kind == "bool":
            req["args"]["points"][0]["x"] = True
        result = b.handle(req)
        assert not result["ok"], result
        assert not calls(repair, "draw") and not calls(repair, "frame")
    finally:
        b.close()


@pytest.mark.parametrize("fault", ["outside", "frame", "duration", "snap", "drop_violation", "origin"])
def test_editor_violation_restored_and_logged(repair, fault):
    b = start(repair, fault, timeout=0.2)
    try:
        before = b.snapshot()
        result = b.handle(op(b))
        assert result["error"] == "verification_violation" and result["restored"], result
        assert b.snapshot().equals(before)
        if fault == "drop_violation":
            assert result["outcome_unknown"]
        rows = [json.loads(line) for line in (repair / "session/ops.jsonl").read_text().splitlines()]
        assert any(row.get("event") == "restore" and row["verified"] for row in rows)
        assert (
            any(row.get("metadata_after") != row.get("metadata_before") for row in rows)
            if fault == "duration"
            else True
        )
        assert b.handle(op(b)) == result
    finally:
        b.close()


@pytest.mark.parametrize(
    "fault,outcome",
    [("drop", "applied_within_mask"), ("delay", "applied_within_mask"), ("drop_unchanged", "unchanged")],
)
def test_unknown_outcome_never_resent(repair, fault, outcome):
    b = start(repair, fault, timeout=0.1)
    try:
        result = b.handle(op(b))
        assert result["ok"] and result["outcome_unknown"] and result["outcome"] == outcome, result
        assert b.handle(op(b)) == result
        assert len(calls(repair, "draw")) == 1
    finally:
        b.close()


@pytest.mark.parametrize(
    "fault,state",
    [
        ("eof", "DISCONNECTED"),
        ("eof_after", "DISCONNECTED"),
        ("not_connected", "DISCONNECTED"),
        ("restore_failure", "TAINTED"),
        ("malformed", "TAINTED"),
    ],
)
def test_terminal_fail_closed_without_disk_write(repair, fault, state):
    b = start(repair, fault)
    before = hashes(repair / "workspace")
    try:
        assert not b.handle(op(b))["ok"]
        assert b.state["state"] == state
        assert not b.handle(op(b, ident="two"))["ok"]
        assert not b.handle({"action": "checkpoint", "token": b.token})["ok"]
        assert hashes(repair / "workspace") == before
        assert calls(repair, "sprite_manage")[-1]["arguments"]["op"] != "save"
    finally:
        b.close()


def test_allowed_ops_and_reads(repair):
    b = start(repair)
    try:
        colour = b.doc["palette"][0]
        operations = [
            ("draw.grid", {"x": 2, "y": 3, "rows": [".."], "legend": {".": None}, "transparent": "erase"}),
            ("draw.grid", {"x": 0, "y": 0, "rows": ["a."], "legend": {"a": colour, ".": None}, "transparent": "erase"}),
            ("draw.blit", {"from": {"x": 0, "y": 0, "width": 2, "height": 1}, "to": {"x": 2, "y": 0}}),
            ("draw.clear", {"region": {"x": 0, "y": 0, "width": 1, "height": 1}}),
            ("cel.move", {"dx": 1, "dy": 0}),
            ("cel.move", {"x": 0, "y": 0}),
        ]
        for i, (operation, args) in enumerate(operations):
            result = b.handle(op(b, str(i), op=operation, args=args))
            assert result["ok"], result
        for tool, args in [
            ("read_pixels", {}),
            ("sprite_info", {}),
            *[("look", {"op": v}) for v in ("ascii", "diff", "preview", "filmstrip", "onion")],
        ]:
            assert b.handle(
                {
                    "action": "read",
                    "token": b.token,
                    "target": {"kind": "still", "direction": "north"},
                    "tool": tool,
                    "args": args,
                }
            )["ok"]
    finally:
        b.close()


def test_checkpoint_export_mismatch_taints(repair):
    b = start(repair, "save_mismatch")
    try:
        assert not b.handle({"action": "checkpoint", "token": b.token})["ok"]
        assert b.state["state"] == "TAINTED"
    finally:
        b.close()


def test_socket_transport_and_cli(repair, tmp_path, capsys):
    import socket

    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.bind(str(tmp_path / "probe.sock"))
        probe.close()
    except PermissionError:
        pytest.skip("execution sandbox prohibits AF_UNIX sockets")
    errors = []

    def run():
        try:
            serve(repair, launcher=launcher(), converter=FakeConverter())
        except Exception as e:
            errors.append(e)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        for _ in range(100):
            if (repair / "session/broker.sock").exists() or errors:
                break
            time.sleep(0.05)
        if errors:
            raise errors[0]
        assert (repair / "session/token").stat().st_mode & 0o777 == 0o600
        assert (repair / "session/broker.sock").stat().st_mode & 0o777 == 0o600
        assert request(repair, {"action": "status"})["ok"]
        spec = tmp_path / "read.json"
        spec.write_text(json.dumps({"target": {"kind": "still", "direction": "north"}, "tool": "read_pixels"}))
        assert main(["repair", "read", str(repair), "--spec", str(spec)]) == 0
        assert json.loads(capsys.readouterr().out)["ok"]
        assert main(["repair", "abort", str(repair)]) == 1
        assert json.loads(capsys.readouterr().out)["error"] == "lease_held"
    finally:
        if not errors:
            request(repair, {"action": "stop", "checkpoint": True})
        thread.join(timeout=10)
    assert not errors and not thread.is_alive()


@pytest.mark.aseprite
def test_live_session(fixture, monkeypatch):  # noqa: F811
    if reason := runtime.unavailable_reason():
        pytest.skip(reason)
    if not os.environ.get("DISPLAY"):
        pytest.skip("nested Xephyr requires a parent DISPLAY")
    repair = prepare(fixture, runtime.AsepriteConverter())
    authorize(repair, authorization(repair))
    b = Broker(repair)
    try:
        assert b.handle(op(b))["ok"]
        before = b.snapshot()
        rejected = b.handle(op(b, "outside", args={"color": b.doc["palette"][0], "points": [{"x": 15, "y": 15}]}))
        assert rejected["error"] == "mask_violation" and b.snapshot().equals(before)
        upstream = b.client.call
        inject = True

        def faulty_draw(name, args, **kwargs):
            nonlocal inject
            if name == "draw" and inject:
                inject = False
                args = {
                    **args,
                    "ops": [
                        *args["ops"],
                        {"kind": "pixels", "color": b.doc["palette"][0], "points": [{"x": 15, "y": 15}]},
                    ],
                }
            return upstream(name, args, **kwargs)

        monkeypatch.setattr(b.client, "call", faulty_draw)
        violated = b.handle(op(b, "editor-violation"))
        assert violated["error"] == "verification_violation" and violated["restored"]
        assert b.snapshot().equals(before)
        assert b.handle({"action": "stop", "token": b.token, "checkpoint": True})["ok"]
    finally:
        b.close()


def test_workspace_ownership_refused(repair, monkeypatch):
    from sonoranlab.repair.authorization import safe_tree

    real = Path.stat
    victim = repair / "workspace/doc.aseprite"

    def stat(path, *args, **kwargs):
        result = real(path, *args, **kwargs)
        if path == victim:
            values = list(result)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "stat", stat)
    with pytest.raises(Denied, match="unsafe_path"):
        safe_tree(repair / "workspace")


@pytest.mark.parametrize("damage", ["truncate", "edit"])
def test_journal_integrity_rejects_restart(repair, damage):
    b = start(repair)
    assert b.handle(op(b))["ok"]
    b.close()
    path = repair / "session/ops.jsonl"
    rows = path.read_text().splitlines()
    if damage == "truncate":
        rows.pop()
    else:
        row = json.loads(rows[0])
        row["event"] = "tampered"
        rows[0] = json.dumps(row)
    path.write_text("\n".join(rows) + "\n")
    with pytest.raises(Denied, match="log_corrupt"):
        start(repair)


def test_crash_reserved_op_id_never_executes_again(repair):
    b = start(repair)
    b.journal.append(event="dispatch", op_id="reserved")
    old_token = b.token
    b.close()  # lifetime lock releases without removing lease/token/log files
    b = start(repair)
    try:
        assert b.token != old_token
        result = b.handle(op(b, "reserved"))
        assert result == {"ok": False, "error": "outcome_unknown"}
        assert not calls(repair, "draw")
    finally:
        b.close()


def test_checkpoint_iteration_survives_crash(repair):
    for iteration in (1, 2):
        b = start(repair)
        try:
            for _ in range(2):
                assert b.handle({"action": "checkpoint", "token": b.token})["ok"]
            assert read_json(repair / "state.json")["iterations"] == iteration
        finally:
            b.close()  # no stop; a checkpoint cannot be erased from the iteration budget
    with pytest.raises(Denied, match="iteration_limit"):
        start(repair)


def test_move_violation_uses_inverse_and_verifies(repair):
    b = start(repair, "outside")
    try:
        before = b.snapshot()
        result = b.handle(op(b, op="cel.move", args={"dx": 1, "dy": 0}))
        assert result["restored"] and b.snapshot().equals(before)
        moves = [r["arguments"] for r in calls(repair, "cel") if r["arguments"]["op"] == "move"]
        assert [(a["dx"], a["dy"]) for a in moves] == [(1, 0), (-1, 0)]
    finally:
        b.close()


def test_undo_and_abort_do_not_touch_inputs(repair, fixture, capsys):  # noqa: F811
    source, parent = hashes(repair / "source"), hashes(fixture[0])
    b = start(repair)
    try:
        assert b.handle(op(b))["ok"]
        assert b.handle(op(b, "undo", op="undo"))["error"] == "operation_not_authorized"
        assert main(["repair", "abort", str(repair)]) == 1
        assert json.loads(capsys.readouterr().out)["error"] == "lease_held"
        assert b.handle({"action": "stop", "token": b.token})["ok"]
    finally:
        b.close()
    assert main(["repair", "abort", str(repair)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "aborted"
    assert hashes(repair / "source") == source and hashes(fixture[0]) == parent


def test_gui_argv_has_only_private_mounts_and_no_editor_on_path(repair, monkeypatch):
    rt = runtime.Runtime(Path("/runtime"), Path("/runtime/aseprite"), Path("/runtime/artist"), Path("/runtime/node"))
    x_socket = Path("/tmp/.X11-unix/X88")
    monkeypatch.setattr(Path, "is_socket", lambda p: p == x_socket)
    args = runtime.bwrap_argv(rt, repair / "workspace", repair / "session/home", mode="gui", x_socket=x_socket)
    mounts = [(args[i + 1], args[i + 2]) for i, a in enumerate(args) if a == "--ro-bind"]
    assert (str(repair / "session/xauth"), "/home/sbx/.Xauthority") in mounts
    assert (str(x_socket), str(x_socket)) in mounts
    assert not any(source == "/tmp/.X11-unix" for source, _ in mounts)
    assert args[args.index("XAUTHORITY") + 1] == "/home/sbx/.Xauthority"
    assert args[args.index("PATH") + 1] == "/opt/node/bin"
    assert args[args.index("TMPDIR") + 1] == "/tmp"
    assert "--clearenv" in args and "--unshare-all" in args and "--new-session" in args
    assert not any(
        v in args
        for v in ("--headless", "--allowLua", "ASEPRITE_AI_HEADLESS", "ASEPRITE_AI_ALLOW_LUA", "ASEPRITE_PATH")
    )
    assert "/work/doc.aseprite" in args[-1] and "node /opt/artist/dist/cli.js serve" in args[-1]
    writable = [(args[i + 1], args[i + 2]) for i, a in enumerate(args) if a == "--bind"]
    assert writable == [(str(repair / "session/home"), "/home/sbx"), (str(repair / "workspace"), "/work")]


def test_detach_second_writer_refused_before_systemd(repair, monkeypatch):
    from sonoranlab.repair.session import detach

    b = start(repair)
    try:
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("must not launch a second daemon"))
        with pytest.raises(Denied, match="lease_held"):
            detach(repair)
    finally:
        b.close()


def test_authorize_cli_and_missing_authorization(fixture, tmp_path, capsys):  # noqa: F811
    repair = prepare(fixture)
    with pytest.raises(Denied, match="authorization_missing"):
        start(repair)
    path = tmp_path / "auth-spec.json"
    path.write_text(json.dumps(authorization(repair)))
    assert main(["repair", "authorize", str(repair), "--spec", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]


def test_detach_command_shape(repair, monkeypatch):
    from sonoranlab.repair.session import detach

    invoked = []

    def run(argv, **kwargs):
        invoked.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    assert detach(repair)["state"] == "starting"
    assert invoked[0][:4] == [
        "systemd-run",
        "--user",
        "--unit",
        "sonoranlab-repair-" + read_json(repair / "repair.json")["repair_id"],
    ]
    assert "--collect" in invoked[0] and invoked[0][-4:] == ["repair", "session", "start", str(repair)]


def test_mcp_write_timeout_is_bounded_and_stream_not_reused():
    from sonoranlab.repair.mcp_client import Disconnected, OutcomeUnknown

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    client = MCPClient(process, timeout=0.1)
    started = time.monotonic()
    try:
        with pytest.raises(OutcomeUnknown):
            client.call("draw", {"payload": "a" * 1024 * 1024})
        assert time.monotonic() - started < 2
        with pytest.raises(Disconnected):
            client.call("sprite_info", {})
    finally:
        client.close()


@pytest.mark.parametrize("checkpoint", [False, True])
def test_disconnected_restart_discards_only_unsaved_edits(repair, checkpoint):
    b = start(repair)
    original = b.accepted.hashes()
    if checkpoint:
        assert b.handle(op(b, "saved"))["ok"]
        assert b.handle({"action": "checkpoint", "token": b.token})["ok"]
        original = b.accepted.hashes()
    assert b.handle(op(b, "unsaved", args={"color": b.doc["palette"][1], "points": [{"x": 1, "y": 0}]}))["ok"]
    assert original != b.accepted.hashes()
    b.client.process.kill()
    b.client.process.wait()
    assert b.handle(op(b, "disconnect"))["state"] == "DISCONNECTED"
    b.close()
    b = start(repair)
    try:
        assert b.accepted.hashes() == original
        assert b.handle(op(b, "fresh"))["ok"]
        rows = [json.loads(line) for line in (repair / "session/ops.jsonl").read_text().splitlines()]
        assert [r for r in rows if r.get("event") == "session_start"][-1]["unsaved_edits_lost"]
    finally:
        b.close()


def test_tainted_restart_still_refused(repair):
    b = start(repair, "restore_failure")
    assert not b.handle(op(b))["ok"]
    b.close()
    with pytest.raises(Denied, match="invalid_state"):
        start(repair)


@pytest.mark.parametrize("fault,ok", [("snapshot_drop", True), ("snapshot_drop_twice", False)])
def test_unknown_snapshot_retried_as_read(repair, fault, ok):
    b = start(repair, fault, timeout=0.1)
    try:
        result = b.handle(op(b))
        assert result["ok"] == ok, result
        if not ok:
            assert result["state"] == "DISCONNECTED"
            assert not calls(repair, "draw")
        # Admission + the failed snapshot + one retry (and verification on success).
        assert len(calls(repair, "sprite_info")) == (4 if ok else 3)
    finally:
        b.close()


def test_nested_request_refused_without_journal_or_server_recursion(repair):
    b = start(repair)
    try:
        nested = None
        for _ in range(1500):
            nested = [nested]
        assert b.handle(op(b, args={"nested": nested}))["error"] == "invalid_request"
        assert b.handle({"action": "status", "token": b.token})["ok"]
        assert b.handle(op(b))["ok"]
    finally:
        b.close()


@pytest.mark.parametrize("args", [{"dx": 17, "dy": 0}, {"dx": 0, "dy": -17}, {"x": 100, "y": 0}])
def test_empty_cel_move_and_excessive_offsets_refused(repair, args):
    b = start(repair)
    try:
        assert b.handle(op(b, "clear", op="draw.clear", args={"region": {"x": 0, "y": 0, "width": 8, "height": 8}}))[
            "ok"
        ]
        assert b.handle(op(b, "huge", op="cel.move", args=args))["error"] == "mask_violation"
        assert b.handle(op(b, "empty", op="cel.move", args={"dx": 1, "dy": 0}))["error"] == "empty_cel"
        assert not [c for c in calls(repair, "cel") if c["arguments"]["op"] == "move"]
        rows = [json.loads(line) for line in (repair / "session/ops.jsonl").read_text().splitlines()]
        verified = next(r for r in rows if r.get("event") == "verification")
        assert {"x", "y"} <= verified["metadata_after"]["cels"]["1"].keys()
    finally:
        b.close()


@pytest.mark.parametrize("flip", [False, True])
def test_blit_outside_source_cel_preserves_destination(repair, flip):
    b = start(repair)
    try:
        assert b.handle(op(b, "seed", args={"color": b.doc["palette"][1], "points": [{"x": 4, "y": 4}]}))["ok"]
        # Source x=0,1 lie outside the cel, including after growing for this destination.
        result = b.handle(
            op(
                b,
                op="draw.blit",
                args={
                    "from": {"x": 0, "y": 3, "width": 3, "height": 1},
                    "to": {"x": 2, "y": 4},
                    "flipHorizontal": flip,
                    "skipTransparent": False,
                },
            )
        )
        assert result["ok"], result
        assert b.accepted.frames["1"][4, 2, 3] == 255
        if flip:
            assert bytes(b.accepted.frames["1"][4, 4, :3]).hex() == b.doc["palette"][1][1:].lower()
    finally:
        b.close()


def test_long_socket_path_refused_before_editor(repair):
    with pytest.raises(Denied, match="socket_path_too_long"):
        serve(repair / ("x" * 108), launcher=lambda p: pytest.fail("editor must not launch"))
    assert not (repair / "session/fake-calls.jsonl").exists()


def test_journal_auth_privacy_and_read_payload_summaries(repair):
    import base64
    import hashlib

    from sonoranlab.repair.authorization import canonical

    b = start(repair)
    try:
        secret = "untrusted-secret-should-not-be-logged"
        assert not b.handle(op(b, token="wrong", args={"secret": secret}))["ok"]
        rows = [json.loads(line) for line in (repair / "session/ops.jsonl").read_text().splitlines()]
        assert set(rows[-1]) == {"action", "error", "time_ns", "previous", "sha256"}
        assert secret not in (repair / "session/ops.jsonl").read_text()
        for tool, args in [("read_pixels", {}), ("look", {"op": "ascii"}), ("look", {"op": "preview"})]:
            result = b.handle(
                {
                    "action": "read",
                    "token": b.token,
                    "tool": tool,
                    "target": {"kind": "still", "direction": "north"},
                    "args": args,
                }
            )
            logged = json.loads((repair / "session/ops.jsonl").read_text().splitlines()[-1])["response"]["result"]
            if tool == "read_pixels":
                raw = canonical(result["result"]["grid"])
                assert logged["grid"] == {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
                assert isinstance(logged["colors"], dict)
            elif args.get("op") == "ascii":
                assert isinstance(logged["gridRows"], dict) and isinstance(logged["text"], dict)
            else:
                raw = base64.b64decode(result["result"]["content"][0]["data"])
                assert logged["content"][0]["data"] == {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
        assert b.token not in (repair / "session/ops.jsonl").read_text()
    finally:
        b.close()


def test_server_survives_deep_json_and_keeps_serving_fake_editor(repair, monkeypatch):
    import io

    from sonoranlab.repair import session

    responses = []
    nested = [b"[" * 40 + b"0" + b"]" * 40 + b"\n", b"[" * 1500 + b"0" + b"]" * 1500 + b"\n"]

    class Connection:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, timeout):
            pass

        def makefile(self, mode):
            return io.BytesIO(self.payload)

        def sendall(self, data):
            responses.append(json.loads(data))

    class Listener:
        def bind(self, path):
            Path(path).touch()

        def listen(self, backlog):
            pass

        def settimeout(self, timeout):
            pass

        def accept(self):
            index = len(responses)
            if index < 2:
                return Connection(nested[index]), None
            token = (repair / "session/token").read_text()
            action = "status" if index == 2 else "stop"
            return Connection(json.dumps({"action": action, "token": token}).encode() + b"\n"), None

        def close(self):
            pass

    monkeypatch.setattr(session.socket, "socket", lambda *args: Listener())
    assert serve(repair, launcher=launcher(), converter=FakeConverter())["stopped"]
    assert [r.get("error") for r in responses[:2]] == ["invalid_request", "invalid_request"]
    assert responses[2]["ok"] and responses[3]["stopped"]
    assert calls(repair, "sprite_info")


def test_xephyr_launch_sets_parent_death_signal(repair, monkeypatch):
    from sonoranlab.repair import session

    launched = []
    signalled = []
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(session, "locate", lambda: None)
    monkeypatch.setattr(session.shutil, "which", lambda tool: tool)
    monkeypatch.setattr(session.subprocess, "check_output", lambda *a, **k: "cookie")
    monkeypatch.setattr(session.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(session, "bwrap_argv", lambda *a, **k: ["fake-editor"])
    monkeypatch.setattr(session, "kill_with_parent", signalled.append)
    real_socket, real_read = Path.is_socket, Path.read_text
    monkeypatch.setattr(Path, "is_socket", lambda path: str(path).startswith("/tmp/.X11-unix/") or real_socket(path))
    monkeypatch.setattr(
        Path, "read_text", lambda path, *a, **k: "123" if str(path).startswith("/tmp/.X") else real_read(path, *a, **k)
    )

    def popen(argv, **kwargs):
        launched.append((argv, kwargs))
        return SimpleNamespace(pid=123, poll=lambda: None)

    monkeypatch.setattr(session.subprocess, "Popen", popen)
    monkeypatch.setattr(session, "Editor", lambda p, x, log: SimpleNamespace(close=log.close))
    editor = session.launch_editor(repair)
    editor.close()
    assert launched[0][0][0] == "Xephyr"
    launched[0][1]["preexec_fn"]()
    assert signalled == [os.getpid()]


def _proc_state(path):
    try:
        return path.read_text().split()[2]
    except (FileNotFoundError, ProcessLookupError):
        return "Z"  # the process vanished between exists() and read: it is dead


def test_parent_death_signal_kills_child():
    import signal

    script = """
import os, subprocess, sys, time
from sonoranlab.repair.session import kill_with_parent
parent = os.getpid()
p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                     preexec_fn=lambda: kill_with_parent(parent))
print(p.pid, flush=True)
time.sleep(60)
"""
    parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    child = None
    try:
        child = int(parent.stdout.readline())
        parent.kill()
        parent.wait(timeout=5)
        for _ in range(100):
            path = Path(f"/proc/{child}/stat")
            if not path.exists() or _proc_state(path) == "Z":
                break
            time.sleep(0.01)
        else:
            pytest.fail("child survived its SIGKILLed parent")
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)
        parent.stdout.close()
        if child and Path(f"/proc/{child}").exists():
            os.kill(child, signal.SIGKILL)


def test_snapshot_timeout_after_dispatch_disconnects_without_resending(repair):
    b = start(repair, "snapshot_after_drop_twice", timeout=0.1)
    try:
        result = b.handle(op(b))
        assert result["state"] == "DISCONNECTED"
        assert len(calls(repair, "draw")) == 1
        assert len(calls(repair, "sprite_info")) == 4
    finally:
        b.close()


def test_self_blit_uses_source_bounds_after_target_growth(repair):
    b = start(repair)
    try:
        # Growing to x=1 brings a transparent source pixel into the source image bounds.
        result = b.handle(
            op(
                b,
                op="draw.blit",
                args={
                    "from": {"x": 1, "y": 3, "width": 2, "height": 1},
                    "to": {"x": 1, "y": 4},
                    "flipHorizontal": True,
                    "skipTransparent": False,
                },
            )
        )
        assert result["ok"], result
        assert b.accepted.frames["1"][4, 1, 3] == 255
        assert b.accepted.frames["1"][4, 2, 3] == 0
    finally:
        b.close()
