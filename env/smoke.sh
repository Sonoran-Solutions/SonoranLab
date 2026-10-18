#!/usr/bin/env bash
# Run one API-format workflow against the sonoranlab-comfy container and record timing + peak memory.
# Works identically online and with `env/comfy.sh up --offline` because the client runs inside the
# container (podman exec) against its own loopback.
#   bash env/smoke.sh workflows/static/klein_base_4b_t2i.json <tag>
# Writes $WORK/runs/<tag>.json (manifest) and prints it. Images land in $WORK/output/p0/.
set -euo pipefail
wf=${1:?workflow.json}; tag=${2:?tag}
WORK="${SONORANLAB_WORK:-$HOME/sonoranlab-work}"; mkdir -p "$WORK/runs"
dev=$(ls -d /sys/class/drm/card*/device | head -1)
mem() { echo "$(cat $dev/mem_info_vram_used) $(cat $dev/mem_info_gtt_used) $(awk '/MemAvailable/{print $2*1024}' /proc/meminfo)"; }
read -r v0 g0 a0 < <(mem)
samples="$WORK/runs/$tag.mem"; : > "$samples"
( while :; do mem >> "$samples"; sleep 0.5; done ) & sampler=$!
trap 'kill $sampler 2>/dev/null || true' EXIT

client=$(cat <<'PY'
import json, sys, time, uuid, urllib.request
wf = json.load(sys.stdin); base = "http://127.0.0.1:8188"
req = urllib.request.Request(base + "/prompt", json.dumps({"prompt": wf, "client_id": str(uuid.uuid4())}).encode(), {"Content-Type": "application/json"})
t0 = time.time()
try:
    pid = json.load(urllib.request.urlopen(req))["prompt_id"]
except urllib.error.HTTPError as e:
    print(json.dumps({"status": "rejected", "error": e.read().decode()[:2000]})); sys.exit(1)
while True:
    h = json.load(urllib.request.urlopen(f"{base}/history/{pid}"))
    if pid in h and h[pid].get("status", {}).get("completed") is not None or (pid in h and h[pid]["status"].get("status_str") == "error"):
        break
    time.sleep(0.5)
t1 = time.time(); e = h[pid]
imgs = [i["subfolder"] + "/" + i["filename"] for o in e["outputs"].values() for i in o.get("images", [])]
msgs = [m for m in e["status"]["messages"] if m[0] in ("execution_error", "execution_interrupted")]
print(json.dumps({"status": e["status"]["status_str"], "prompt_id": pid, "wall_s": round(t1 - t0, 2), "images": imgs, "errors": msgs}))
PY
)
set +e; res=$(podman exec -i sonoranlab-comfy python -c "$client" < "$wf"); rc=$?; set -e
kill $sampler; trap - EXIT
python3 - "$wf" "$tag" "$res" "$samples" "$v0" "$g0" "$a0" "$WORK" <<'PY' | tee "$WORK/runs/$tag.json"
import json, sys, hashlib, subprocess, datetime, os
wf, tag, res, samples, v0, g0, a0, work = sys.argv[1:]
r = json.loads(res) if res.strip() else {"status": "no-response"}
s = [list(map(int, l.split())) for l in open(samples) if len(l.split()) == 3]
peak = lambda i: max(x[i] for x in s) if s else None
r.update({
  "tag": tag, "date": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
  "workflow": wf, "workflow_sha256": hashlib.sha256(open(wf, "rb").read()).hexdigest(),
  "image": subprocess.run(["podman", "inspect", "-f", "{{.ImageName}} {{.Image}}", "sonoranlab-comfy"], capture_output=True, text=True).stdout.strip(),
  "network": subprocess.run(["podman", "inspect", "-f", "{{.HostConfig.NetworkMode}}", "sonoranlab-comfy"], capture_output=True, text=True).stdout.strip(),
  "gpu_vram_used_gib": {"before": round(int(v0)/2**30, 2), "peak": round(peak(0)/2**30, 2) if s else None},
  "gpu_gtt_used_gib": {"before": round(int(g0)/2**30, 2), "peak": round(peak(1)/2**30, 2) if s else None},
  "host_mem_available_gib": {"before": round(int(a0)/2**30, 2), "min": round(min(x[2] for x in s)/2**30, 2) if s else None},
  "output_sha256": {i: hashlib.sha256(open(os.path.join(work, "output", i.lstrip("/")), "rb").read()).hexdigest() for i in r.get("images", []) if os.path.exists(os.path.join(work, "output", i.lstrip("/")))},
})
print(json.dumps(r, indent=2))
PY
exit $rc
