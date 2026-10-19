#!/usr/bin/env bash
# Run one API-format workflow against the sonoranlab-comfy container and record timing + peak memory.
# Works identically online and with `env/comfy.sh up --offline` because the client runs inside the
# container (podman exec) against its own loopback.
#   bash env/smoke.sh workflows/static/klein_base_4b_t2i.json <tag>
# Writes $WORK/runs/<tag>.json (manifest) and prints it. Images land in $WORK/output/p0/.
# SMOKE_TIMEOUT defaults to 1800 seconds; SMOKE_EXPECT_IMAGES defaults to 1.
# Guarded by default: unless already inside env/runwindow.py, this re-runs itself through it with
# --need-gib ${SMOKE_NEED_GIB:-60} (Qwen Edit single prompt; use 64 for batched Qwen, 30 for klein / node chain)
# and --allow-pause for each unit in SMOKE_ALLOW_PAUSE (space-separated, owner-approved only).
# SMOKE_SETTLE_S overrides the run window's post-pause settle wait.
# SONORANLAB_UNGUARDED=1 skips the guard (tests only).
# SMOKE_GTT_CREDIT=1 opts in to --gtt-credit (heuristic; default off).
# Exit codes: 0 success, 2 rejected, 3 execution_error, 4 timeout,
#             5 missing_outputs, 6 http_error (or no-response); from the guard: 3 refused, 4 another window active.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
read -r -a PODMAN <<<"${SONORANLAB_PODMAN:-podman}"   # test hook
wf=${1:?workflow.json}; tag=${2:?tag}
# Guarded only if our *direct* parent is the run window (an inherited/leaked variable doesn't count).
if [[ "${SONORANLAB_RUNWINDOW:-}" != "$PPID" && "${SONORANLAB_UNGUARDED:-}" != 1 ]]; then
  allow=(); for u in ${SMOKE_ALLOW_PAUSE:-}; do allow+=(--allow-pause "$u"); done
  [[ "${SMOKE_GTT_CREDIT:-0}" == 1 ]] && allow+=(--gtt-credit)
  settle=(); [[ -n "${SMOKE_SETTLE_S:-}" ]] && settle=(--settle-s "$SMOKE_SETTLE_S")
  exec python3 "$HERE/runwindow.py" --need-gib "${SMOKE_NEED_GIB:-60}" "${allow[@]}" "${settle[@]}" -- bash "$HERE/smoke.sh" "$wf" "$tag"
fi
WORK="${SONORANLAB_WORK:-$HOME/sonoranlab-work}"; mkdir -p "$WORK/runs"
dev=$(ls -d /sys/class/drm/card*/device 2>/dev/null | head -1 || true)   # absent on CI runners: memory reads as 0
gpu() { cat "$dev/$1" 2>/dev/null || echo 0; }
mem() { echo "$(gpu mem_info_vram_used) $(gpu mem_info_gtt_used) $(awk '/MemAvailable/{print $2*1024}' /proc/meminfo)"; }
read -r v0 g0 a0 < <(mem)
samples="$WORK/runs/$tag.mem"; : > "$samples"
( while :; do mem >> "$samples"; sleep 0.5; done ) & sampler=$!
cleanup() {
  if [[ -n "$sampler" ]]; then
    kill "$sampler" 2>/dev/null || true
    wait "$sampler" 2>/dev/null || true
    sampler=
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

set +e
res=$("${PODMAN[@]}" exec -i sonoranlab-comfy python -c "$(cat "$HERE/comfy_client.py")" --output-dir /work/output --timeout "${SMOKE_TIMEOUT:-1800}" --expect-images "${SMOKE_EXPECT_IMAGES:-1}" < "$wf")
rc=$?
set -e
cleanup
if [[ -z "${res//[[:space:]]/}" ]]; then rc=6; fi
python3 - "$wf" "$tag" "$res" "$samples" "$v0" "$g0" "$a0" "$WORK" "$rc" <<'PY' | tee "$WORK/runs/$tag.json"
import json, sys, hashlib, subprocess, datetime, os
wf, tag, res, samples, v0, g0, a0, work, rc = sys.argv[1:]
r = json.loads(res) if res.strip() else {"status": "no-response", "exit_code": 6}
r.setdefault("exit_code", int(rc))
s = [list(map(int, l.split())) for l in open(samples) if len(l.split()) == 3]
peak = lambda i: max(x[i] for x in s) if s else None
output_dir = os.path.realpath(os.path.join(work, "output"))
output_hashes = {}
for image in r.get("images", []):
    path = os.path.realpath(os.path.join(output_dir, image))
    if os.path.commonpath([output_dir, path]) == output_dir and os.path.isfile(path):
        output_hashes[image] = hashlib.sha256(open(path, "rb").read()).hexdigest()
r.update({
  "tag": tag, "date": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
  "workflow": wf, "workflow_sha256": hashlib.sha256(open(wf, "rb").read()).hexdigest(),
  "image": subprocess.run([*os.environ.get("SONORANLAB_PODMAN", "podman").split(), "inspect", "-f", "{{.ImageName}} {{.Image}}", "sonoranlab-comfy"], capture_output=True, text=True).stdout.strip(),
  "network": subprocess.run([*os.environ.get("SONORANLAB_PODMAN", "podman").split(), "inspect", "-f", "{{.HostConfig.NetworkMode}}", "sonoranlab-comfy"], capture_output=True, text=True).stdout.strip(),
  "gpu_vram_used_gib": {"before": round(int(v0)/2**30, 2), "peak": round(peak(0)/2**30, 2) if s else None},
  "gpu_gtt_used_gib": {"before": round(int(g0)/2**30, 2), "peak": round(peak(1)/2**30, 2) if s else None},
  "host_mem_available_gib": {"before": round(int(a0)/2**30, 2), "min": round(min(x[2] for x in s)/2**30, 2) if s else None},
  "output_sha256": output_hashes,
})
print(json.dumps(r, indent=2))
PY
exit $rc
