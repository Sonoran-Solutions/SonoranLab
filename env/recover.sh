#!/usr/bin/env bash
# Phase 0 recovery: from a freshly booted box to a verified Qwen Edit image, following phase0-env.md "Start / recover".
# Times every step and compares the output hash with the reference run. Log: ~/sonoranlab-work/recovery.log
#   bash env/recover.sh [--allow-pause UNIT ...]   (run from either checkout of the repo)
# The Qwen check runs inside env/runwindow.py: it refuses (exit 3) if the box lacks ~60 GiB available and only
# pauses user units you name with --allow-pause (e.g. llama-gemory); each paused unit is restored to its original
# state afterwards, including on failure or Ctrl-C, and a run killed outright is restored on the next invocation.
# If ~/sonoranlab-work/reboot-started exists (epoch seconds written just before `systemctl reboot`), the total
# elapsed time since the reboot was issued is reported too.
set -uo pipefail
cd "$(dirname "$0")/.."
WORK="${SONORANLAB_WORK:-$HOME/sonoranlab-work}"; mkdir -p "$WORK"; LOG="$WORK/recovery.log"
REF=fea3c1496b6916b02974d2c130fba3e93b02ed57f67e60b5fb43c45426518c19   # qwen_edit_online_run1/2, offline run
t0=$(date +%s); exec > >(tee -a "$LOG") 2>&1
step() { local s=$(date +%s) name=$1; echo "== $name"; shift; "$@"; local rc=$?; echo "   rc=$rc ($(( $(date +%s) - s )) s)"
  (( rc == 0 )) || { echo "== RECOVERY FAIL at: $name (rc=$rc)"; exit 1; }; }
quiet() { "$@" >/dev/null; }   # silence a command's stdout without hiding step's own log lines
allow=(); while [[ $# -gt 0 ]]; do
  case "$1" in --allow-pause) allow+=(--allow-pause "$2"); shift 2 ;; *) echo "unknown option: $1" >&2; exit 2 ;; esac
done
echo "### recovery $(date -Is) boot=$(uptime -s) repo=$PWD rev=$(git rev-parse --short HEAD 2>/dev/null)"
step "restore units left paused by an interrupted run" python3 env/runwindow.py --restore --drain-s 60
step "image present (else rebuild from env/Containerfile)" bash -c 'podman image exists localhost/sonoranlab-comfy:p0 && echo present || podman build -t localhost/sonoranlab-comfy:p0 -f env/Containerfile env/'
step "weights verify (sha256, no network)" quiet bash env/fetch_models.sh --verify
step "ComfyUI up (refuses to replace a busy container)" quiet bash env/comfy.sh up --replace
step "Qwen Edit smoke in a guarded run window" quiet python3 env/runwindow.py --need-gib 52 "${allow[@]}" -- \
  bash env/smoke.sh workflows/static/qwen_edit_2511_fp8_lightning_4step.json recovery_qwen_edit
got=$(python3 -c "import json;print(*json.load(open('$WORK/runs/recovery_qwen_edit.json'))['output_sha256'].values())")
[[ "$got" == "$REF" ]] && verdict=PASS || verdict="FAIL (output $got != $REF)"
echo "== runbook time: $(( $(date +%s) - t0 )) s"
[[ -f "$WORK/reboot-started" ]] && echo "== since reboot issued: $(( $(date +%s) - $(cat "$WORK/reboot-started") )) s (boot finished $(uptime -s))"
echo "== RECOVERY $verdict"
[[ "$verdict" == PASS ]]
