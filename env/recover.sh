#!/usr/bin/env bash
# Phase 0 recovery: from a freshly booted box to a verified Qwen Edit image, following phase0-env.md "Start / recover".
# Times every step and compares the output hash with the reference run. Log: ~/sonoranlab-work/recovery.log
#   bash env/recover.sh            (run from either checkout of the repo)
# If ~/sonoranlab-work/reboot-started exists (epoch seconds written just before `systemctl reboot`), the total
# elapsed time since the reboot was issued is reported too.
set -uo pipefail
cd "$(dirname "$0")/.."
WORK="${SONORANLAB_WORK:-$HOME/sonoranlab-work}"; mkdir -p "$WORK"; LOG="$WORK/recovery.log"
REF=fea3c1496b6916b02974d2c130fba3e93b02ed57f67e60b5fb43c45426518c19   # qwen_edit_online_run1/2, offline run
t0=$(date +%s); exec > >(tee -a "$LOG") 2>&1
step() { local s=$(date +%s); echo "== $1"; shift; "$@"; local rc=$?; echo "   rc=$rc ($(( $(date +%s) - s )) s)"; return $rc; }
echo "### recovery $(date -Is) boot=$(uptime -s) repo=$PWD rev=$(git rev-parse --short HEAD 2>/dev/null)"
step "image present (else rebuild from env/Containerfile)" bash -c 'podman image exists localhost/sonoranlab-comfy:p0 && echo present || podman build -t localhost/sonoranlab-comfy:p0 -f env/Containerfile env/' || exit 1
step "weights verify (sha256, no network)" bash env/fetch_models.sh --verify >/dev/null || exit 1
step "ComfyUI up" bash env/comfy.sh up >/dev/null || exit 1
step "Qwen Edit smoke (llama-gemory paused)" bash -c 'systemctl --user stop llama-gemory 2>/dev/null; bash env/smoke.sh workflows/static/qwen_edit_2511_fp8_lightning_4step.json recovery_qwen_edit >/dev/null; rc=$?; systemctl --user start llama-gemory 2>/dev/null; exit $rc' || exit 1
got=$(python3 -c "import json;print(*json.load(open('$WORK/runs/recovery_qwen_edit.json'))['output_sha256'].values())")
[[ "$got" == "$REF" ]] && verdict=PASS || verdict="FAIL (output $got != $REF)"
echo "== runbook time: $(( $(date +%s) - t0 )) s"
[[ -f "$WORK/reboot-started" ]] && echo "== since reboot issued: $(( $(date +%s) - $(cat "$WORK/reboot-started") )) s (boot finished $(uptime -s))"
echo "== RECOVERY $verdict"
[[ "$verdict" == PASS ]]
