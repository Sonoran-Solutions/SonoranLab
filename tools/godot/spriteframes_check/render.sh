#!/usr/bin/env bash
# Run render_animation.gd with a real renderer but no window on the user's desktop (same private headless GNOME Shell +
# llvmpipe set-up as tools/godot/bake.sh).
# Usage: render.sh <imported project copy> -- <render_animation.gd options>
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
godot="${GODOT:-$HOME/.local/bin/godot}"
proj="$(cd "$1" && pwd)"; shift
[[ "${1:-}" == "--" ]] && shift
cp "$here/render_animation.gd" "$proj/"
priv="$(mktemp -d "${TMPDIR:-/tmp}/slrender.XXXXXX")"
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
gnome-shell --headless --wayland --no-x11 --wayland-display=slrender --virtual-monitor 256x256 \
  >"$SLR_PRIV/shell.log" 2>&1 & shell=$!
for _ in $(seq 100); do [[ -S "$XDG_RUNTIME_DIR/slrender" ]] && break; sleep 0.1; done
[[ -S "$XDG_RUNTIME_DIR/slrender" ]] || { echo "render.sh: compositor did not start" >&2; cat "$SLR_PRIV/shell.log" >&2; exit 3; }
rc=0
WAYLAND_DISPLAY=slrender "$SLR_GODOT" --path "$SLR_PROJ" --display-driver wayland --rendering-driver opengl3 \
  --audio-driver Dummy --script res://render_animation.gd -- "$@" || rc=$?
kill "$shell" 2>/dev/null; wait "$shell" 2>/dev/null || true
exit $rc'
env -i HOME="$HOME" PATH="$PATH" LANG="${LANG:-C.UTF-8}" USER="$USER" \
  XDG_RUNTIME_DIR="$priv/run" XDG_CONFIG_HOME="$priv/cfg" XDG_DATA_HOME="$priv/data" XDG_CACHE_HOME="$priv/cache" \
  GSETTINGS_BACKEND=memory LIBGL_ALWAYS_SOFTWARE=1 \
  SLR_PRIV="$priv" SLR_GODOT="$godot" SLR_PROJ="$proj" \
  dbus-run-session --config-file="$priv/bus.conf" -- bash -c "$inner" render "$@"
