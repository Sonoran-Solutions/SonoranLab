#!/usr/bin/env bash
# Run bake_rig.gd with a real renderer but no window on the user's desktop: a private, headless
# GNOME Shell (mutter) compositor on its own D-Bus session with no activatable services, its own
# XDG dirs and runtime dir, memory GSettings; Godot draws into it with Mesa llvmpipe (CPU).
# Usage: tools/godot/bake.sh -- <bake_rig.gd options>   (see bake_rig.gd header)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
godot="${GODOT:-$HOME/.local/bin/godot}"
[[ "${1:-}" == "--" ]] && shift
priv="$(mktemp -d "${TMPDIR:-/tmp}/slbake.XXXXXX")"
trap 'rm -rf "$priv"' EXIT
mkdir -p "$priv"/{run,cfg,data,cache} && chmod 700 "$priv/run"
cat > "$priv/bus.conf" <<'CONF'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig><type>session</type><listen>unix:tmpdir=/tmp</listen>
<policy context="default"><allow send_destination="*" eavesdrop="true"/><allow eavesdrop="true"/>
<allow own="*"/></policy></busconfig>
CONF
inner='
gnome-shell --headless --wayland --no-x11 --wayland-display=slbake --virtual-monitor 256x256 \
  >"$SLBAKE_PRIV/shell.log" 2>&1 & shell=$!
for _ in $(seq 100); do [[ -S "$XDG_RUNTIME_DIR/slbake" ]] && break; sleep 0.1; done
[[ -S "$XDG_RUNTIME_DIR/slbake" ]] || { echo "bake.sh: compositor did not start" >&2; cat "$SLBAKE_PRIV/shell.log" >&2; exit 3; }
rc=0
WAYLAND_DISPLAY=slbake "$SLBAKE_GODOT" --path "$SLBAKE_HERE" --display-driver wayland --rendering-driver opengl3 \
  --audio-driver Dummy --script res://bake_rig.gd -- "$@" || rc=$?
kill "$shell" 2>/dev/null; wait "$shell" 2>/dev/null || true
exit $rc'
env -i HOME="$HOME" PATH="$PATH" LANG="${LANG:-C.UTF-8}" USER="$USER" \
  XDG_RUNTIME_DIR="$priv/run" XDG_CONFIG_HOME="$priv/cfg" XDG_DATA_HOME="$priv/data" XDG_CACHE_HOME="$priv/cache" \
  GSETTINGS_BACKEND=memory LIBGL_ALWAYS_SOFTWARE=1 \
  SLBAKE_PRIV="$priv" SLBAKE_GODOT="$godot" SLBAKE_HERE="$here" \
  dbus-run-session --config-file="$priv/bus.conf" -- bash -c "$inner" bake "$@"
