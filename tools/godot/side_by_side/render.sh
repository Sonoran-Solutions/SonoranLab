#!/usr/bin/env bash
# Run render_plan.gd with a real renderer but no window on the user's desktop: a private headless GNOME Shell + llvmpipe
# (same set-up as tools/godot/spriteframes_check/render.sh), with every XDG directory under $SLR_PRIV so Godot, GNOME Shell
# and D-Bus write nothing outside the job's OUT directory. `sonoranlab smoke-render` runs this under its own supervisor
# (finite timeout, process-group kill, bounded output); do not run it by hand without a timeout.
# Env: SLR_PRIV (existing dir, private to this run), SLR_GODOT (godot binary), SLR_PROJ (generated project dir).
# Usage: render.sh -- <render_plan.gd options>        Exit 3: compositor did not start (engine unavailable).
set -euo pipefail
[[ "${1:-}" == "--" ]] && shift
: "${SLR_PRIV:?}" "${SLR_GODOT:?}" "${SLR_PROJ:?}"
ulimit -f 65536   # no single file (shell log, Godot log, PNG) above 64 MiB
mkdir -p "$SLR_PRIV"/{run,cfg,data,cache} && chmod 700 "$SLR_PRIV/run"
cat > "$SLR_PRIV/bus.conf" <<'CONF'
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
[[ -S "$XDG_RUNTIME_DIR/slrender" ]] || { echo "render.sh: compositor did not start" >&2; head -c 4000 "$SLR_PRIV/shell.log" >&2; exit 3; }
rc=0
WAYLAND_DISPLAY=slrender "$SLR_GODOT" --path "$SLR_PROJ" --display-driver wayland --rendering-driver opengl3 \
  --audio-driver Dummy --script res://render_plan.gd -- "$@" || rc=$?
kill "$shell" 2>/dev/null; wait "$shell" 2>/dev/null || true
exit $rc'
env -i HOME="$HOME" PATH="$PATH" LANG="${LANG:-C.UTF-8}" USER="${USER:-sonoranlab}" \
  XDG_RUNTIME_DIR="$SLR_PRIV/run" XDG_CONFIG_HOME="$SLR_PRIV/cfg" XDG_DATA_HOME="$SLR_PRIV/data" XDG_CACHE_HOME="$SLR_PRIV/cache" \
  GSETTINGS_BACKEND=memory LIBGL_ALWAYS_SOFTWARE=1 \
  SLR_PRIV="$SLR_PRIV" SLR_GODOT="$SLR_GODOT" SLR_PROJ="$SLR_PROJ" \
  dbus-run-session --config-file="$SLR_PRIV/bus.conf" -- bash -c "$inner" render "$@"
