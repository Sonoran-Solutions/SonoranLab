"""Mailbox contract with the independent fake editor (socket transport can be sandboxed out)."""

import io
import json
import threading
import time
from pathlib import Path

import pytest
from test_broker import authorization, calls, op, start
from test_interchange import fixture, prepare  # noqa: F401

from sonoranlab.cli import main
from sonoranlab.repair import Refused, session
from sonoranlab.repair import relay as module
from sonoranlab.repair.authorization import Denied, authorize, canonical


@pytest.fixture
def repair(fixture):  # noqa: F811
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
    (agent / "inbox").mkdir(parents=True)
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
        agent.mkdir()
        if where == "outbox":
            (agent / "outbox").symlink_to(outside, target_is_directory=True)
        else:
            (agent / "outbox").mkdir()
            target = outside / "request.json"
            target.write_text('{"action":"status"}')
            (agent / "outbox/request.json").symlink_to(target)
    monkeypatch.setattr(module, "request", lambda _, payload: b.handle({**payload, "token": b.token}))
    try:
        with pytest.raises((Refused, Denied), match=r"symlink|unsafe_path"):
            module.relay(repair, agent)
        assert not list(outside.glob("*.png"))
    finally:
        b.close()


@pytest.mark.parametrize("where", ["repair", "parent", "repo", "ancestor"])
def test_relay_requires_external_agent_dir(repair, where):
    doc = json.loads((repair / "repair.json").read_text())
    path = {
        "repair": repair / "worker",
        "parent": Path(doc["parent_job"]) / "worker",
        "repo": Path(module.__file__).resolve().parents[2] / "worker",
        "ancestor": repair.parent,
    }[where]
    with pytest.raises(Denied, match="unsafe_agent_dir"):
        module.relay(repair, path)
    assert not (path / "outbox").exists()


def test_relay_cli(repair, tmp_path, monkeypatch, capsys):
    agent = tmp_path / "worker"
    monkeypatch.setattr(module, "relay", lambda r, a: {"ok": r == repair and a == agent, "stopped": True})
    assert main(["repair", "relay", str(repair), "--agent-dir", str(agent)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "stopped": True}
