#!/usr/bin/env bash
# DYL-131/133 actual game-consumer check: runs the GAME's OWN entry points against a DISPOSABLE snapshot that has a
# SonoranLab delivery bundle applied (`sonoranlab deliver apply BUNDLE --target SNAPSHOT`). Never the real checkout.
#
#   GODOT=/path/to/godot-4.7-mono SMOKE_DISPLAY=:N tools/game_consumer/run.sh --snapshot SNAP --bundle BUNDLE \
#       --out OUT --test-class SamuraiPresentationTests [--test-class ...] [--fallback-render --sibling A --sibling B]
#
# Consumer steps (all required for a pass), each supervised: own process group, wall-clock deadline, TERM -> 5 s ->
# KILL of the whole group, output drained into OUT/logs/<step>.{head,tail}.log (first 1 MiB + rolling 4 MiB tail,
# stopped at 64 MiB produced), OUT/logs/<step>.status:
#   1 dotnet_test      dotnet test SNAP/tests/TwosAreUp.Combat.Tests -c Release --filter FullyQualifiedName~CLASS|...
#                      (>= 1 executed test required; SamuraiPresentationTests is the one that reads characters.json)  600 s
#   2 game_build       dotnet build SNAP/TwosAreUp.csproj -c Debug: the assembly the Godot .NET editor binary loads
#                      when it runs a project (.godot/mono/temp/bin/Debug)                                            600 s
#   3 godot_import     godot --headless --path SNAP --import (one re-run only on Godot 4.7.2's singleton crash)       300 s
#   4 pixel_check      godot --headless --path SNAP -- --pixel-check (RotationLab -> PixelArtLab.Check +
#                      PixelUnitChecks.Check). A failure whose message is exactly "<id>: clip table" for a sheet
#                      without a death clip is the known missing clip, reported as such; anything else fails.       600 s
#   5 gallery_capture  godot --path SNAP --display-driver x11 -- --pixel-gallery-capture=OUT/gallery on the nested X
#                      display $SMOKE_DISPLAY (hold the game's nested-display lock, as for its own smokes). The
#                      gallery shows PixelGallery's fixed Layout, so this proves the patched manifest does not break
#                      the real gallery; it does NOT draw the new id (it is not in Layout). Budget 128 MiB of PNGs. 900 s
# Optional, NOT a consumer check (labelled `fallback_render_non_consumer`, never gates the result):
#   6 fallback_render  --fallback-render: godot/ConsumerCheck.cs + .tscn copied into SNAP/sonoranlab_consumer_check/,
#                      game rebuilt, then PixelStandee + the real shader for the new id beside two siblings, every
#                      row x (still + every clip frame) at 1x/4x into OUT/fallback (64 MiB). Removed afterwards.  600+900 s
# Godot runs with XDG_{DATA,CONFIG,CACHE}_HOME in a private dir under OUT (its user:// logs included), which is measured
# and deleted at the end. Result: OUT/result.json (`sonoranlab.game_consumer_result/1`), exit 0 iff pass. Then:
#   sonoranlab deliver verify BUNDLE --target SNAP --consumer-result OUT/result.json
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
die() { echo "run.sh: $*" >&2; exit 2; }

snap="" bundle="" out="" tests=() sibs=() fallback=false
while (($#)); do
  case "$1" in
    --snapshot) snap="$2"; shift 2 ;;
    --bundle) bundle="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    --test-class) tests+=("$2"); shift 2 ;;
    --sibling) sibs+=("$2"); shift 2 ;;
    --fallback-render) fallback=true; shift ;;
    *) die "unknown argument $1" ;;
  esac
done
[[ -n "$snap" && -n "$bundle" && -n "$out" ]] || die "--snapshot, --bundle and --out are required"
((${#tests[@]} >= 1)) || die "at least one --test-class (zero selected tests is not a pass)"
! $fallback || ((${#sibs[@]} == 2)) || die "--fallback-render needs exactly two --sibling ids"

snap="$(realpath -e "$snap")" || die "--snapshot does not exist"
bundle="$(realpath -e "$bundle")" || die "--bundle does not exist"
[[ -f "$bundle/bundle.json" ]] || die "$bundle has no bundle.json"
for p in "$HOME/Projects/TwosAreUp" ${SONORANLAB_PROTECTED:-}; do   # the real checkout is never a snapshot
  prot="$(realpath -m "$p")"
  [[ "$snap" == "$prot" || "$snap" == "$prot"/* ]] && die "refusing the protected game checkout $prot"
done
[[ "$(git -C "$snap" rev-parse --show-toplevel 2>/dev/null)" == "$snap" ]] || die "$snap is not a git worktree root"
[[ -e "$out" ]] && die "--out $out exists (results are never overwritten)"
out="$(realpath -m "$out")"
[[ "$out" == "$snap" || "$out" == "$snap"/* ]] && die "--out must be outside the snapshot"
harness="$snap/sonoranlab_consumer_check"
[[ -e "$harness" ]] && die "$harness exists (another run in progress or a leftover)"
avail=$(df -P --output=avail -B1 "$(dirname "$out")" | tail -n 1)
((avail >= 20 * 1024 ** 3)) || die "less than 20 GiB free under $(dirname "$out")"
csproj="$snap/TwosAreUp.csproj"
[[ -f "$csproj" ]] || die "$csproj missing"
[[ -n "${SMOKE_DISPLAY:-}" ]] || die "set SMOKE_DISPLAY to the nested X display (held under the game's display lock)"
godot="${GODOT:-}"; [[ -n "$godot" ]] || die "set GODOT to the Godot 4.7 .NET (mono) editor binary"
"$godot" --version 2>/dev/null | grep -qi mono || die "$godot is not a Godot .NET (mono) build"
command -v dotnet >/dev/null || die "dotnet not on PATH"
character="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["character"])' "$bundle/bundle.json")"

mkdir -p "$out"/{logs,dotnet,gallery} && priv="$out/priv" && mkdir -p "$priv"/{cfg,data,cache} || die "cannot create $out"
python3 -I "$here/summarize.py" pre "$bundle" "$snap" "$out" || die "cannot read the snapshot state"
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 MSBUILDDISABLENODEREUSE=1 DOTNET_CLI_DO_NOT_USE_MSBUILD_SERVER=1
export XDG_CONFIG_HOME="$priv/cfg" XDG_DATA_HOME="$priv/data" XDG_CACHE_HOME="$priv/cache"
build=(-nodeReuse:false -p:UseSharedCompilation=false)

cleanup() {
  rm -rf -- "$harness"
  [[ -d "$priv" ]] && { du -sb "$priv" | cut -f1 > "$out/logs/private_bytes"; rm -rf -- "$priv"; }
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# supervise NAME DEADLINE_S CMD...: own process group, deadline, flood stop, group kill + reap, OUT/logs/NAME.status
supervise() {
  local name=$1 deadline=$2; shift 2
  local prefix="$out/logs/$name" start=$SECONDS timed_out=false flooded=false rc logger pid
  rm -f "$prefix.fifo" && mkfifo "$prefix.fifo"
  python3 -I "$here/bounded_log.py" "$prefix" < "$prefix.fifo" & logger=$!
  setsid "$@" > "$prefix.fifo" 2>&1 < /dev/null & pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    ((SECONDS - start >= deadline)) && { timed_out=true; break; }
    [[ -e "$prefix.flood" ]] && { flooded=true; break; }
    sleep 1
  done
  if $timed_out || $flooded; then
    kill -TERM -- "-$pid" 2>/dev/null; sleep 5; kill -KILL -- "-$pid" 2>/dev/null
  fi
  wait "$pid"; rc=$?
  kill -KILL -- "-$pid" 2>/dev/null   # descendants left in the job's group
  for _ in $(seq 10); do kill -0 "$logger" 2>/dev/null || break; sleep 1; done
  kill "$logger" 2>/dev/null; wait "$logger" 2>/dev/null; rm -f "$prefix.fifo"
  printf '{"exit": %d, "timed_out": %s, "flooded": %s, "seconds": %d, "deadline_s": %d, "produced_bytes": %s}\n' \
    "$rc" "$timed_out" "$flooded" "$((SECONDS - start))" "$deadline" "$(cat "$prefix.bytes" 2>/dev/null || echo null)" \
    > "$prefix.status"
  echo "run.sh: $name exit $rc (timed_out=$timed_out flooded=$flooded, $((SECONDS - start)) s)" >&2
  return "$rc"
}

filter="$(printf 'FullyQualifiedName~%s|' "${tests[@]}")"; filter="${filter%|}"
supervise dotnet_test 600 dotnet test "$snap/tests/TwosAreUp.Combat.Tests" -c Release --filter "$filter" \
  --logger "trx;LogFileName=tests.trx" --results-directory "$out/dotnet" "${build[@]}"

if supervise game_build 600 dotnet build "$csproj" -c Debug "${build[@]}"; then
  supervise godot_import 300 "$godot" --headless --path "$snap" --import
  if (($? == 134)) && grep -q 'Parameter "singleton" is null' "$out/logs/godot_import".*.log \
      && grep -q is_cmdline_mode "$out/logs/godot_import".*.log; then   # DYL-101, exact signature only
    mv "$out/logs/godot_import.status" "$out/logs/godot_import.attempt1.status"
    supervise godot_import 300 "$godot" --headless --path "$snap" --import
  fi
  supervise pixel_check 600 "$godot" --headless --path "$snap" -- --pixel-check
  supervise gallery_capture 900 env DISPLAY="$SMOKE_DISPLAY" "$godot" --path "$snap" --display-driver x11 \
    --rendering-driver opengl3 --audio-driver Dummy -- "--pixel-gallery-capture=$out/gallery"
  if $fallback; then   # NOT a consumer check: SonoranLab's own scene, labelled as such in result.json
    mkdir -p "$out/fallback" "$harness" && cp "$here/godot/ConsumerCheck.cs" "$here/godot/consumer_check.tscn" "$harness/"
    supervise fallback_build 600 dotnet build "$csproj" -c Debug "${build[@]}" \
      && supervise fallback_render 900 env DISPLAY="$SMOKE_DISPLAY" "$godot" --path "$snap" --display-driver x11 \
        --rendering-driver opengl3 --audio-driver Dummy --scene res://sonoranlab_consumer_check/consumer_check.tscn \
        -- --character "$character" --siblings "${sibs[0]},${sibs[1]}" --out "$out/fallback" --max-bytes $((64 << 20))
    rm -rf -- "$harness"
    supervise fallback_unbuild 600 dotnet build "$csproj" -c Debug "${build[@]}"   # drop the harness from the assembly
  fi
fi
rm -rf -- "$harness"
python3 -I - "$snap" "$out" <<'PY'   # keep a copy of any bundle file the engine rewrote (evidence)
import hashlib, json, shutil, sys
from pathlib import Path
snap, out = (Path(a) for a in sys.argv[1:])
for rel, sha in json.loads((out / "pre.json").read_text())["files_sha256"].items():
    p = snap / rel
    if p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() != sha:
        (out / "rewritten" / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, out / "rewritten" / rel)
PY
python3 -I "$here/summarize.py" final "$bundle" "$snap" "$out"
