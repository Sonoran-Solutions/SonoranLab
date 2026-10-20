"""Deterministic ancestor swaps at reservation and publication boundaries."""

import json
import os
import shutil

import pytest
from test_confine import snapshot
from test_finalize import mod as finalizer
from test_finalize import saved  # noqa: F401
from test_interchange import fixture  # noqa: F401

from sonoranlab.repair import Refused, confine
from sonoranlab.repair import relay as relaymod
from sonoranlab.repair import workspace as ws


@pytest.mark.parametrize("command", ["prepare", "finalize", "relay", "reuse"])
def test_ancestor_swap_before_open(saved, tmp_path, monkeypatch, command):  # noqa: F811
    repair, parent, profile, _ = saved
    staging = tmp_path / "staging"
    hop = staging / "hop"
    hop.mkdir(parents=True)
    out = hop / "new"
    game = tmp_path / "game"
    monkeypatch.setenv("SONORANLAB_REPAIR_ROOT", str(staging))
    monkeypatch.setenv("SONORANLAB_AGENT_ROOT", str(staging))
    monkeypatch.setattr(relaymod, "request", lambda *args: {"session_state": "STOPPED"})
    if command == "reuse":
        relaymod.relay(repair, out)
        shutil.copytree(out, game / "new")  # A plausible mailbox behind the swapped symlink.
    before = snapshot(game)
    monkeypatch.setattr(relaymod, "request", lambda *args: pytest.fail("broker contacted after race"))
    original = confine._open_directory

    def swap(path, **kwargs):
        hop.rename(staging / "old-hop")
        hop.symlink_to(game, target_is_directory=True)
        return original(path, **kwargs)

    monkeypatch.setattr(confine, "_open_directory", swap)
    with pytest.raises(Refused, match="race_detected"):
        if command == "prepare":
            ws.prepare(parent, profile, "hero", "idle", out, converter=ws.FakeConverter())
        elif command == "finalize":
            finalizer.finalize(repair, out)
        else:
            relaymod.relay(repair, out)
    assert snapshot(game) == before


@pytest.mark.parametrize("command,kind", [("prepare", "repair"), ("finalize", "child"), ("relay", "mailbox")])
def test_successful_creation_is_marked(saved, tmp_path, monkeypatch, command, kind):  # noqa: F811
    repair, parent, profile, _ = saved
    out = tmp_path / "missing/root/new"
    if command == "prepare":
        assert ws.prepare(parent, profile, "hero", "idle", out, converter=ws.FakeConverter())["ok"]
    elif command == "finalize":
        assert finalizer.finalize(repair, out)["pass"]
    else:
        monkeypatch.setenv("SONORANLAB_AGENT_ROOT", str(tmp_path))
        monkeypatch.setattr(relaymod, "request", lambda *args: {"session_state": "STOPPED"})
        assert relaymod.relay(repair, out)["ok"]
    marker = out / ".sonoranlab-staging"
    data = json.loads(marker.read_text())
    assert set(data) == {"kind", "id", "created_at"}
    assert data["kind"] == kind and data["id"] and data["created_at"]
    assert marker.stat().st_mode & 0o777 == 0o600


def test_failed_prepare_protects_nested_destination(fixture):  # noqa: F811
    parent, profile, out = fixture

    class Failing(ws.FakeConverter):
        def build(self, workspace):
            assert (workspace.parent / ".sonoranlab-staging").is_file()
            raise RuntimeError("editor failed")

    with pytest.raises(RuntimeError, match="editor failed"):
        ws.prepare(parent, profile, "hero", "idle", out, converter=Failing())
    assert not (out / "repair.json").exists()
    with pytest.raises(Refused, match="nested_workspace"):
        ws.prepare(parent, profile, "hero", "idle", out / "nested", converter=ws.FakeConverter())
    assert not (out / "nested").exists()


@pytest.mark.parametrize("change", ["move", "replace", "git", "workspace"])
def test_verify_detects_changed_destination(tmp_path, change):
    out, fd = confine.create_destination(tmp_path / "new", root=tmp_path, kind="repair", id="test")
    try:
        if change in ("move", "replace"):
            out.rename(tmp_path / "moved")
            if change == "replace":
                out.mkdir()
        elif change == "git":
            (tmp_path / ".git").write_text("gitdir: /elsewhere")
        else:
            (tmp_path / "state.json").write_text("{}")
        with pytest.raises(Refused, match="race_detected"):
            confine.verify_destination(out, fd)
    finally:
        os.close(fd)


@pytest.mark.parametrize("command", ["prepare", "finalize"])
def test_final_gate_refuses_moved_directory(saved, tmp_path, monkeypatch, command):  # noqa: F811
    repair, parent, profile, _ = saved
    out = tmp_path / "new"
    original = ws.write_json
    last = "repair.json" if command == "prepare" else "repair-manifest.json"

    def move_at_publication(directory, name, value):
        result = original(directory, name, value)
        if directory == out and name == last:
            out.rename(tmp_path / "moved")
            out.mkdir()  # Retain an unrelated replacement; never remove it on refusal.
            (out / "keep").write_text("keep")
        return result

    monkeypatch.setattr(ws if command == "prepare" else finalizer.artifacts, "write_json", move_at_publication)
    with pytest.raises(Refused, match="race_detected"):
        if command == "prepare":
            ws.prepare(parent, profile, "hero", "idle", out, converter=ws.FakeConverter())
        else:
            finalizer.finalize(repair, out)
    assert (out / "keep").read_text() == "keep"


def test_relay_verifies_again_before_forwarding(saved, tmp_path, monkeypatch):  # noqa: F811
    repair, _, _, _ = saved
    monkeypatch.setenv("SONORANLAB_AGENT_ROOT", str(tmp_path))
    hop = tmp_path / "hop"
    hop.mkdir()
    out = hop / "mailbox"
    monkeypatch.setattr(relaymod, "request", lambda *args: {"session_state": "STOPPED"})
    relaymod.relay(repair, out)
    (out / "outbox/request.json").write_text('{"action":"status"}')
    (repair / "state.json").write_text('{"session_state":"ACTIVE"}')
    calls = []

    def request(path, payload):
        calls.append(payload)
        return {"session_state": "ACTIVE"}

    original = relaymod.rename_new

    def swap_after_claim(*args):
        original(*args)
        hop.rename(tmp_path / "old-hop")
        hop.symlink_to(tmp_path / "game", target_is_directory=True)

    monkeypatch.setattr(relaymod, "request", request)
    monkeypatch.setattr(relaymod, "rename_new", swap_after_claim)
    with pytest.raises(Refused, match="race_detected"):
        relaymod.relay(repair, out)
    assert calls == [{"action": "status"}]  # Initial contact preceded the swap; no worker request followed it.
    assert not list((tmp_path / "game").iterdir())


def test_swap_after_parent_fd_open_marks_only_pinned_directory(tmp_path, monkeypatch):
    hop = tmp_path / "hop"
    hop.mkdir()
    game = tmp_path / "game"
    game.mkdir()
    original = os.mkdir

    def swap(name, *args, **kwargs):
        if name == "new" and kwargs.get("dir_fd") is not None:
            hop.rename(tmp_path / "old-hop")
            hop.symlink_to(game, target_is_directory=True)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", swap)
    with pytest.raises(Refused, match="race_detected"):
        confine.create_destination(hop / "new", root=tmp_path, kind="repair", id="test")
    assert not list(game.iterdir())
    assert (tmp_path / "old-hop/new/.sonoranlab-staging").is_file()
