"""Pinned, external editor runtime and the single sandbox argv shared by batch and future GUI sessions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sonoranlab.artifacts import sha256_file
from sonoranlab.repair import EditorUnavailable, Refused

ASEPRITE = "1.3.18.6"
ARTIST = "0.8.2"
NODE = "26.8.1"
EXTENSION_SCRIPT = "/opt/artist/extension/ai-artist.lua"
LUA = Path(__file__).parent / "lua" / "interchange.lua"


def no_symlinks(path: Path) -> Path:
    """Check before resolving, including directory ancestors (also catches dangling links)."""
    path = Path(os.path.abspath(path))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise Refused(f"{path}: symlinked input/path refused")
    return path


@dataclass(frozen=True)
class Runtime:
    root: Path
    aseprite: Path
    artist: Path
    node: Path

    def record(self) -> dict:
        """Versions identify the pinned distributions; hashes identify their exact installed bytes."""
        return {
            "root": str(self.root),
            "aseprite": {"version": ASEPRITE, "sha256": sha256_file(self.aseprite / "usr/bin/aseprite")},
            "node": {"version": NODE, "sha256": sha256_file(self.node / "bin/node")},
            "artist": {"version": ARTIST, "package_lock_sha256": sha256_file(self.artist / "package-lock.json")},
        }


def locate(root: Path | None = None) -> Runtime:
    """Validate the installed layout without executing any editor/third-party code on the host."""
    root = no_symlinks(
        Path(root or os.environ.get("SONORANLAB_EDITOR_RUNTIME", "~/sonoranlab-work/phase3.5/runtime")).expanduser()
    )
    runtime = Runtime(
        root, root / f"aseprite-{ASEPRITE}/squashfs-root", root / f"aseprite-ai-artist-{ARTIST}", root / f"node-v{NODE}"
    )
    for p in (
        runtime.aseprite / "usr/bin/aseprite",
        runtime.node / "bin/node",
        runtime.artist / "package-lock.json",
        runtime.artist / "package.json",
        runtime.artist / "dist/cli.js",
        runtime.artist / "extension/ai-artist.lua",
    ):
        if not no_symlinks(p).is_file():
            raise EditorUnavailable(f"editor runtime missing: {p}")
    for p in (runtime.aseprite / "usr/bin/aseprite", runtime.node / "bin/node"):
        if not os.access(p, os.X_OK):
            raise EditorUnavailable(f"editor runtime is not executable: {p}")
    for name in ("package.json", "package-lock.json"):
        package = json.loads((runtime.artist / name).read_text())
        if not isinstance(package, dict) or package.get("version") != ARTIST:
            raise EditorUnavailable(f"editor runtime {name} is not pinned version {ARTIST}")
    if not shutil.which("bwrap"):
        raise EditorUnavailable("bubblewrap (bwrap) is unavailable")
    return runtime


def _base_argv() -> list[str]:
    return [
        "bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--symlink",
        "usr/bin",
        "/bin",
        "--ro-bind",
        "/etc",
        "/etc",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]


def unavailable_reason() -> str | None:
    """Probe the required namespace isolation too; never fall back to an unsandboxed editor."""
    try:
        locate()
        result = subprocess.run(
            [*_base_argv(), "--clearenv", "/usr/bin/true"], capture_output=True, text=True, timeout=10
        )
        if result.returncode:
            return f"bubblewrap unavailable: {result.stderr.strip()}"
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return str(e)
    return None


def seed_home(home: Path, extension_script: str = EXTENSION_SCRIPT) -> Path:
    """Seed only a private HOME; permission is keyed to the extension's *sandbox* path, never its contents."""
    home = no_symlinks(home)
    if home.name != "home" or home.parent.name != "session":
        raise Refused("private home must be <repair>/session/home")
    home.mkdir(parents=True, mode=0o700, exist_ok=True)
    home.chmod(0o700)
    config = no_symlinks(home / ".config/aseprite")
    config.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(extension_script.encode(), usedforsecurity=False).hexdigest()
    ini = config / "aseprite.ini"
    expected = f"[script_access]\n{key} = 31\n"
    if ini.exists():
        no_symlinks(ini)
        if ini.read_text() != expected:
            raise Refused(f"{ini}: existing private config differs")
    else:
        with ini.open("x") as f:
            f.write(expected)
        ini.chmod(0o600)
    return home


def bwrap_argv(
    runtime: Runtime,
    workspace: Path,
    home: Path,
    *,
    mode: str = "batch",
    action: str = "build",
    output: str = "export",
    x_socket: Path | None = None,
) -> list[str]:
    """Only /work and the private HOME are host-writable. GUI binds exactly one nested Xephyr socket."""
    workspace, home = no_symlinks(workspace), no_symlinks(home)
    if workspace.name != "workspace" or home != workspace.parent / "session/home":
        raise Refused("expected <repair>/workspace and <repair>/session/home")
    for p in (workspace, home):
        if not p.is_dir() or p.is_relative_to(runtime.root) or runtime.root.is_relative_to(p):
            raise Refused(f"unsafe sandbox mount: {p}")
    argv = [
        *_base_argv(),
        "--ro-bind",
        str(runtime.aseprite),
        "/opt/aseprite",
        "--ro-bind",
        str(runtime.artist),
        "/opt/artist",
        "--ro-bind",
        str(runtime.node),
        "/opt/node",
        "--ro-bind",
        str(LUA),
        "/opt/interchange.lua",
        "--bind",
        str(home),
        "/home/sbx",
        "--bind",
        str(workspace),
        "/work",
    ]
    if mode == "gui":
        if x_socket is None or not re.fullmatch(r"X\d+", x_socket.name) or not x_socket.is_socket():
            raise Refused("GUI requires a nested Xephyr X socket")
        argv += [
            "--dir",
            "/tmp/.X11-unix",
            "--ro-bind",
            str(x_socket),
            f"/tmp/.X11-unix/{x_socket.name}",
            "--ro-bind",
            str(no_symlinks(home.parent / "xauth")),
            "/home/sbx/.Xauthority",
            "--ro-bind",
            str(runtime.artist / "extension"),
            "/home/sbx/.config/aseprite/extensions/aseprite-ai-artist",
        ]
    elif mode != "batch":
        raise Refused(f"unknown sandbox mode: {mode}")
    argv += [
        "--clearenv",
        "--setenv",
        "HOME",
        "/home/sbx",
        "--setenv",
        "PATH",
        "/opt/node/bin" if mode == "gui" else "/usr/bin",
        "--setenv",
        "ASEPRITE_USER_FOLDER",
        "/home/sbx/.config/aseprite",
        "--setenv",
        "LD_LIBRARY_PATH",
        "/opt/aseprite/usr/lib",
        "--chdir",
        "/work",
    ]
    if mode == "gui":
        argv += [
            "--setenv",
            "DISPLAY",
            f":{x_socket.name[1:]}",
            "--setenv",
            "XAUTHORITY",
            "/home/sbx/.Xauthority",
            "--setenv",
            "TMPDIR",
            "/tmp",
            "/bin/sh",
            "-c",
            "/opt/aseprite/usr/bin/aseprite /work/doc.aseprite >/work/aseprite.log 2>&1 & "
            "exec /opt/node/bin/node /opt/artist/dist/cli.js serve",
        ]
    else:
        if action not in {"build", "export"} or not re.fullmatch(r"export(?:-\d{4,})?", output):
            raise Refused("invalid fixed interchange action/output")
        argv += [
            "/opt/aseprite/usr/bin/aseprite",
            "-b",
            "--script-param",
            f"action={action}",
            "--script-param",
            f"output={output}",
            "--script",
            "/opt/interchange.lua",
        ]
    return argv


class AsepriteConverter:
    """Explicit batch conversion, always inside the same containment as a future live editor."""

    def __init__(self) -> None:
        if reason := unavailable_reason():
            raise EditorUnavailable(reason)
        self.runtime = locate()

    def record(self) -> dict:
        return self.runtime.record()

    def _run(self, workspace: Path, action: str, output: str = "export") -> None:
        result = subprocess.run(
            bwrap_argv(self.runtime, workspace, workspace.parent / "session/home", action=action, output=output),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode:
            raise Refused(f"sandboxed Aseprite {action} failed: {result.stderr.strip()} {result.stdout.strip()}")

    def build(self, workspace: Path) -> None:
        self._run(workspace, "build")
        if not (workspace / "doc.aseprite").is_file():
            raise Refused("Aseprite did not build doc.aseprite")

    def export(self, workspace: Path, output: Path) -> None:
        self._run(workspace, "export", output.name)
