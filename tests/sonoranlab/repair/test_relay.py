"""Mailbox contract with the independent fake editor (socket transport can be sandboxed out)."""

import io
import json
import os
import threading
import time
from pathlib import Path

import pytest
from test_broker import authorization, calls, op, start
from test_confine import snapshot
from test_interchange import fixture, prepare  # noqa: F401

from sonoranlab import artifacts
from sonoranlab.cli import main
from sonoranlab.repair import Refused, session
from sonoranlab.repair import relay as module
from sonoranlab.repair.authorization import Denied, authorize, canonical


@pytest.fixture
def repair(fixture, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SONORANLAB_AGENT_ROOT", str(tmp_path))
    monkeypatch.setenv("SONORANLAB_REPAIR_ROOT", str(tmp_path))
    path = prepare(fixture)
    authorize(path, authorization(path))
    return path


def wait_for(path, errors):
    for _ in range(200):
        if path.exists():
            return
        if errors:
            raise errors[0]
        time.sleep(0.01)
    pytest.fail(f"relay did not produce {path}")


def run_relay(repair, agent, b, monkeypatch):
    forwarded = []
    real_request = module.request

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, timeout):
            pass

        def connect(self, path):
            assert path == str(repair / "session/broker.sock")

        def sendall(self, data):
            payload = json.loads(data)
            assert payload["token"] == b.token
            self.response = b.handle(payload)

        def makefile(self, mode):
            return io.BytesIO(canonical(self.response) + b"\n")

    monkeypatch.setattr(session.socket, "socket", lambda *args: Socket())

    def transport(path, payload):
        assert path == repair
        forwarded.append(payload.copy())
        return real_request(path, payload)

    monkeypatch.setattr(module, "request", transport)
    errors = []
    results = []

    def run():
        try:
            results.append(module.relay(repair, agent))
        except Exception as e:
            errors.append(e)

    thread = threading.Thread(target=run)
    thread.start()
    wait_for(agent / "outbox", errors)
    return thread, errors, results, forwarded


def submit(agent, name, payload):
    # Workers publish a complete request via rename, too.
    temp = agent / "outbox" / ".pending"
    temp.write_text(json.dumps(payload))
    temp.rename(agent / "outbox" / (name + ".json"))


def create_mailbox(repair, agent, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(module, "request", lambda *args: {"session_state": "STOPPED"})
        assert module.relay(repair, agent) == {"ok": True, "stopped": True}


def test_relay_ops_reads_token_privacy_bad_names_and_stop(repair, tmp_path, monkeypatch):
    b = start(repair)
    agent = tmp_path / "worker"
    thread, errors, results, forwarded = run_relay(repair, agent, b, monkeypatch)
    try:
        payload = op(b)
        payload["token"] = "worker-supplied-wrong-token"
        submit(agent, "edit", payload)
        wait_for(agent / "inbox/edit.json", errors)
        assert json.loads((agent / "inbox/edit.json").read_text())["ok"]
        for view in ("preview", "filmstrip", "onion"):
            submit(
                agent,
                view,
                {
                    "action": "read",
                    "target": {"kind": "still", "direction": "north"},
                    "tool": "look",
                    "args": {"op": view},
                },
            )
            wait_for(agent / f"inbox/{view}.json", errors)
            result = json.loads((agent / f"inbox/{view}.json").read_text())
            image = result["result"]["content"][0]
            assert image["path"] == view + ".png" and "data" not in image
            assert (agent / "inbox" / image["path"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        for action in ("checkpoint", "stop", "abort", "session", "unknown"):
            submit(agent, action, {"action": action})
            wait_for(agent / f"inbox/{action}.json", errors)
            assert json.loads((agent / f"inbox/{action}.json").read_text())["error"] == "action_not_relayable"
        for name in ("Bad.json", "-bad.json", "a" * 65 + ".json", "bad.txt"):
            (agent / "outbox" / name).write_text('{"action":"stop"}')
        (agent / "outbox/large.json").write_bytes(b" " * (module.MAX_FILE + 1))
        (agent / "outbox/directory.json").mkdir()
        submit(agent, "status", {"action": "status"})
        wait_for(agent / "inbox/status.json", errors)
        assert all(p.name not in {"Bad.json", "-bad.json", "large.json"} for p in (agent / "inbox").iterdir())
        assert all("token" not in payload for payload in forwarded)
        assert {payload["action"] for payload in forwarded} == {"op", "read", "status"}
        assert len(calls(repair, "draw")) == 1
        assert not [c for c in calls(repair, "sprite_manage") if c["arguments"]["op"] == "save"]
        assert all(b.token.encode() not in p.read_bytes() for p in agent.rglob("*") if p.is_file())
    finally:
        b.handle({"action": "stop", "token": b.token})
        b.close()
        thread.join(timeout=5)
    assert not errors and not thread.is_alive()
    assert results == [{"ok": True, "stopped": True}]


@pytest.mark.parametrize("suffix", ["json", "png"])
def test_relay_refuses_overwrite_before_dispatch(repair, tmp_path, monkeypatch, suffix):
    b = start(repair)
    agent = tmp_path / "worker"
    create_mailbox(repair, agent, monkeypatch)
    previous = agent / f"inbox/edit.{suffix}"
    previous.write_text("keep me")
    thread, errors, _, _ = run_relay(repair, agent, b, monkeypatch)
    try:
        submit(agent, "edit", {k: v for k, v in op(b).items() if k != "token"})
        thread.join(timeout=5)
        assert not thread.is_alive() and len(errors) == 1
        assert isinstance(errors[0], Denied) and errors[0].code == "relay_output_exists"
        assert previous.read_text() == "keep me" and not calls(repair, "draw")
    finally:
        b.close()


@pytest.mark.parametrize("where", ["root", "outbox", "file"])
def test_relay_refuses_symlinks(repair, tmp_path, monkeypatch, where):
    b = start(repair)
    agent = tmp_path / "worker"
    outside = tmp_path / "outside"
    outside.mkdir()
    if where == "root":
        agent.symlink_to(outside, target_is_directory=True)
    else:
        create_mailbox(repair, agent, monkeypatch)
        if where == "outbox":
            (agent / "outbox").rmdir()
            (agent / "outbox").symlink_to(outside, target_is_directory=True)
        else:
            target = outside / "request.json"
            target.write_text('{"action":"status"}')
            (agent / "outbox/request.json").symlink_to(target)
    monkeypatch.setattr(module, "request", lambda _, payload: b.handle({**payload, "token": b.token}))
    try:
        with pytest.raises((Refused, Denied), match=r"symlink|unsafe_path|invalid_mailbox"):
            module.relay(repair, agent)
        assert not list(outside.glob("*.png"))
    finally:
        b.close()


@pytest.mark.parametrize(
    "where",
    [
        "game",
        "game_checkout",
        "palette_checkout",
        "repo",
        "repair",
        "parent",
        "ancestor",
        "outside_root",
        "symlink_component",
        "unmarked",
        "other_repair",
        "invalid_marker",
        "marker_symlink",
        "outbox_symlink",
        "inbox_file",
        "missing_directory",
    ],
)
def test_relay_refusals_create_nothing_and_never_contact_broker(repair, tmp_path, monkeypatch, where):
    doc = json.loads((repair / "repair.json").read_text())
    game = tmp_path / "game"
    repo = tmp_path / "sonoranlab"
    repo.mkdir()
    monkeypatch.setattr(artifacts, "_REPO", repo)
    path = {
        "game": game / "nested/worker",
        "game_checkout": game / "nested/worker",
        "palette_checkout": tmp_path / "palette-source/nested/worker",
        "repo": repo / "nested/worker",
        "repair": repair / "worker",
        "parent": Path(doc["parent_job"]) / "worker",
        "ancestor": repair.parent,
        "outside_root": tmp_path / "elsewhere/nested/worker",
        "symlink_component": tmp_path / "link/nested/worker",
    }.get(where, tmp_path / "worker")
    if where in ("game_checkout", "palette_checkout"):
        checkout = game if where == "game_checkout" else tmp_path / "palette-source"
        (checkout / ".git").mkdir(parents=True)
        (checkout / ".git/HEAD").write_text("ref: refs/heads/main\n")
        if where == "palette_checkout":
            profile = Path(doc["profile"]["path"])
            (checkout / "palette.png").write_bytes((tmp_path / "palette.png").read_bytes())
            profile.write_text(profile.read_text().replace("source: palette.png", "source: palette-source/palette.png"))
    elif where == "outside_root":
        monkeypatch.setenv("SONORANLAB_AGENT_ROOT", str(tmp_path / "agents"))
    elif where == "symlink_component":
        (tmp_path / "link").symlink_to(game, target_is_directory=True)
    elif where == "unmarked":
        path.mkdir()
    elif where in (
        "other_repair",
        "invalid_marker",
        "marker_symlink",
        "outbox_symlink",
        "inbox_file",
        "missing_directory",
    ):
        create_mailbox(repair, path, monkeypatch)
        marker = path / ".sonoranlab-mailbox"
        if where == "other_repair":
            marker.write_text(json.dumps({"repair_id": "another-repair"}))
        elif where == "invalid_marker":
            marker.write_text("not json")
        elif where == "marker_symlink":
            marker.unlink()
            marker.symlink_to(repair / "repair.json")
        elif where == "outbox_symlink":
            (path / "outbox").rmdir()
            (path / "outbox").symlink_to(game, target_is_directory=True)
        elif where == "inbox_file":
            (path / "inbox").rmdir()
            (path / "inbox").write_text("keep me")
        elif where == "missing_directory":
            (path / "processing").rmdir()
    monkeypatch.setattr(module, "request", lambda *args: pytest.fail("broker must not be contacted"))
    before = snapshot(tmp_path)
    with pytest.raises(Refused):
        module.relay(repair, path)
    assert snapshot(tmp_path) == before


def test_relay_reuses_its_mailbox_for_the_same_repair(repair, tmp_path, monkeypatch):
    agent = tmp_path / "worker"
    create_mailbox(repair, agent, monkeypatch)
    (agent / "TASK.md").write_text("iteration 2 task")  # the worker's own files live beside the queues
    (agent / "reference").mkdir()
    marker = agent / ".sonoranlab-mailbox"
    repair_id = json.loads((repair / "repair.json").read_text())["repair_id"]
    assert json.loads(marker.read_text()) == {"repair_id": repair_id}
    assert marker.stat().st_mode & 0o777 == 0o600
    forwarded = []

    def status(path, payload):
        forwarded.append((path, payload))
        return {"session_state": "STOPPED"}

    monkeypatch.setattr(module, "request", status)
    before = snapshot(agent)
    assert module.relay(repair, agent) == {"ok": True, "stopped": True}
    assert forwarded == [(repair, {"action": "status"})]
    assert snapshot(agent) == before


@pytest.mark.parametrize("queue", ["outbox", "processing", "inbox"])
@pytest.mark.parametrize(
    "marker",
    [
        "manifest.json",
        "review.json",
        "report.json",
        "repair.json",
        "mapping.json",
        "state.json",
        "authorization.json",
        "repair-manifest.json",
        ".sonoranlab-staging",
        "marker_symlink",
        "raw+processed",
    ],
)
def test_relay_refuses_job_layout_in_queue_before_contact(repair, tmp_path, monkeypatch, queue, marker):
    agent = tmp_path / "worker"
    create_mailbox(repair, agent, monkeypatch)
    if marker == "raw+processed":
        for child in ("raw", "processed"):
            (agent / queue / child).mkdir()
    elif marker == "marker_symlink":
        (agent / queue / "review.json").symlink_to(tmp_path / "missing")
    else:
        (agent / queue / marker).write_text("{}")
    monkeypatch.setattr(module, "request", lambda *args: pytest.fail("broker must not be contacted"))
    before = snapshot(agent)
    assert module.relay(repair, agent) == {"ok": False, "error": "unsafe_mailbox"}
    assert snapshot(agent) == before


@pytest.mark.parametrize("queue", ["outbox", "processing", "inbox"])
@pytest.mark.parametrize("change", ["move", "replace", "symlink", "marker", "raw+processed"])
def test_relay_stops_before_publishing_after_queue_changes(repair, tmp_path, monkeypatch, queue, change):
    agent = tmp_path / "worker"
    create_mailbox(repair, agent, monkeypatch)
    (repair / "state.json").write_text('{"session_state":"ACTIVE"}')
    submit(agent, "first", {"action": "status"})
    submit(agent, "second", {"action": "status"})
    moved = tmp_path / "game" / queue
    forwarded = []
    after = None

    def request(path, payload):
        nonlocal after
        forwarded.append(payload)
        if len(forwarded) == 2:  # Initial status poll, then the first worker request.
            if change == "marker":
                (agent / queue / "review.json").write_text("{}")
            elif change == "raw+processed":
                for child in ("raw", "processed"):
                    (agent / queue / child).mkdir()
            else:
                (agent / queue).rename(moved)
                if change == "replace":
                    (agent / queue).mkdir()
                elif change == "symlink":
                    (agent / queue).symlink_to(moved, target_is_directory=True)
            (repair / "state.json").write_text('{"session_state":"STOPPED"}')
            after = snapshot(tmp_path)
            return {"ok": True, "session_state": "STOPPED"}
        return {"session_state": "ACTIVE"}

    monkeypatch.setattr(module, "request", request)
    error = "unsafe_mailbox" if change in ("marker", "raw+processed") else "race_detected"
    assert module.relay(repair, agent) == {"ok": False, "error": error}
    assert forwarded == [{"action": "status"}, {"action": "status"}]
    # The relay must not write or clean up after detection.
    assert snapshot(tmp_path) == after


def test_relay_cli(repair, tmp_path, monkeypatch, capsys):
    agent = tmp_path / "worker"
    monkeypatch.setattr(module, "relay", lambda r, a: {"ok": r == repair and a == agent, "stopped": True})
    assert main(["repair", "relay", str(repair), "--agent-dir", str(agent)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "stopped": True}


def test_reuse_allows_worker_files_but_needs_marker(repair, tmp_path, monkeypatch):
    """Iteration 2 reuses the mailbox that also holds the worker's task files; only the queues + marker are required."""
    from sonoranlab.repair.relay import relay as run_relay

    agent = Path(os.environ["SONORANLAB_AGENT_ROOT"]) / "worker"
    agent.mkdir(parents=True)
    for name in ("outbox", "processing", "inbox"):
        (agent / name).mkdir()
    (agent / "TASK.md").write_text("task")
    with pytest.raises(Denied, match="invalid_mailbox"):
        run_relay(repair, agent)  # no marker: not a mailbox this relay created
    assert not (agent / ".sonoranlab-mailbox").exists()


@pytest.mark.parametrize("queue", ["outbox", "processing", "inbox"])
@pytest.mark.parametrize("kind", ["checkout", "bare"])
def test_git_repository_in_a_queue_is_refused_before_broker_contact(repair, tmp_path, monkeypatch, queue, kind):
    """Re-audit: a queue that is a Git repository (git init before reuse) must not be relayed into or mutated."""
    import subprocess

    agent = tmp_path / "worker"
    create_mailbox(repair, agent, monkeypatch)
    args = ["git", "init", "-q"] + (["--bare"] if kind == "bare" else []) + [str(agent / queue)]
    subprocess.run(args, check=True)
    (agent / "outbox" / "query.json").write_text(json.dumps({"action": "status"}))
    monkeypatch.setattr(module, "request", lambda *a: pytest.fail("broker must not be contacted"))
    before = snapshot(agent)
    result = module.relay(repair, agent)
    assert result["ok"] is False and result["error"] == "unsafe_mailbox", result
    assert snapshot(agent) == before
