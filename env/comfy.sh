#!/usr/bin/env bash
# SonoranLab ComfyUI container (kyuz0 Strix Halo toolbox, pinned by digest).
# Runs alongside the owner's host ComfyUI (:8188) without touching it.
#   env/comfy.sh up [--offline] [--replace] [--force]
#       start on 127.0.0.1:8189 (--offline: --network=none, API reachable only via `exec`).
#       An existing running container is never replaced silently: `up` refuses (exit 5) unless --replace, and
#       refuses even with --replace while ComfyUI has a running/pending job or can't report its queue, unless --force.
#   env/comfy.sh down [--force]   same busy check
#   env/comfy.sh status | logs | exec <cmd...>
set -euo pipefail

read -r -a PODMAN <<<"${SONORANLAB_PODMAN:-podman}"   # test hook

NAME=sonoranlab-comfy
# Pinned toolbox + Phase 0 node packs, built from env/Containerfile (base digest pinned there).
IMAGE="${SONORANLAB_COMFY_IMAGE:-localhost/sonoranlab-comfy:p0}"
HERE="$(cd "$(dirname "$0")" && pwd)"
MODELS="${SONORANLAB_MODELS:-$HOME/comfy-models}"   # local NVMe only; never the CIFS share
WORK="${SONORANLAB_WORK:-$HOME/sonoranlab-work}"    # outputs + generated config
PORT="${SONORANLAB_PORT:-8189}"
# Unified memory is shared with the desktop and other AI services. Cap the container (no swap) and make it the
# preferred OOM victim, so an overshooting load kills ComfyUI instead of systemd-oomd killing a desktop app scope.
MEM="${SONORANLAB_MEM:-64g}"
# Toolbox-documented flags (start_comfy_ui alias / benchmark_workflows.py). Change one at a time and record it.
FLAGS="${SONORANLAB_COMFY_FLAGS:---disable-mmap --gpu-only --disable-smart-memory --cache-none --bf16-vae}"

running() { "${PODMAN[@]}" container exists "$NAME" && [[ "$("${PODMAN[@]}" inspect -f '{{.State.Running}}' "$NAME")" == true ]]; }

# Exit 5 unless it is safe to remove the current container. $1=replace-approved(0/1) $2=force(0/1)
guard_replace() {
  running || return 0
  if [[ "$1" != 1 ]]; then
    echo "refusing: container $NAME is already running; pass --replace to recreate it" >&2; exit 5
  fi
  [[ "$2" == 1 ]] && return 0
  local q
  if ! q=$("${PODMAN[@]}" exec "$NAME" curl -sf -m 5 http://127.0.0.1:8188/queue 2>/dev/null); then
    echo "refusing: cannot read the ComfyUI queue of $NAME (it may be mid-load); pass --force to destroy it anyway" >&2; exit 5
  fi
  if ! python3 -c 'import json,sys; q=json.load(sys.stdin); sys.exit(1 if q["queue_running"] or q["queue_pending"] else 0)' <<<"$q"; then
    echo "refusing: $NAME has a running or pending generation; wait for it or pass --force" >&2; exit 5
  fi
}

up() {
  local net=(-p "127.0.0.1:${PORT}:8188") replace=0 force=0
  for arg in "$@"; do
    case "$arg" in
      --offline) net=(--network=none) ;;
      --replace) replace=1 ;;
      --force) replace=1; force=1 ;;
      *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
  done
  guard_replace "$replace" "$force"
  mkdir -p "$WORK/output" "$MODELS"/{checkpoints,text_encoders,vae,diffusion_models,unet,loras,controlnet,clip_vision,latent_upscale_models}
  cat > "$WORK/extra_model_paths.yaml" <<YAML
sonoranlab:
    base_path: /models
    checkpoints: checkpoints
    text_encoders: text_encoders
    clip: text_encoders
    vae: vae
    diffusion_models: diffusion_models
    unet: unet
    loras: loras
    controlnet: controlnet
    clip_vision: clip_vision
    latent_upscale_models: latent_upscale_models
YAML
  "${PODMAN[@]}" image exists "$IMAGE" || "${PODMAN[@]}" build -t "$IMAGE" -f "$HERE/Containerfile" "$HERE"
  # ComfyUI-RMBG rewrites birefnet.py in place on load, so it gets a writable copy; the verified originals stay untouched.
  mkdir -p "$MODELS/RMBG" "$MODELS/controlnet_aux_ckpts" "$WORK/RMBG"
  cp -a -u "$MODELS/RMBG/." "$WORK/RMBG/"
  "${PODMAN[@]}" rm -f "$NAME" >/dev/null 2>&1 || true
  # Own systemd scope for conmon/port forwarder, so killing the launching terminal's scope (e.g. an oomd kill of
  # the app that hosts the terminal) doesn't orphan the container.
  local scope=(); [[ "${SONORANLAB_SCOPE:-1}" == 1 ]] && command -v systemd-run >/dev/null && scope=(systemd-run --user --scope --quiet --collect)
  "${scope[@]}" "${PODMAN[@]}" run -d --name "$NAME" "${net[@]}" \
    --device /dev/kfd --device /dev/dri --group-add keep-groups \
    --security-opt seccomp=unconfined \
    --memory "$MEM" --memory-swap "$MEM" --oom-score-adj 1000 \
    -v "$MODELS:/models:ro" \
    -v "$MODELS/controlnet_aux_ckpts:/opt/ComfyUI/custom_nodes/comfyui_controlnet_aux/ckpts:ro" \
    -v "$WORK/RMBG:/opt/ComfyUI/models/RMBG" \
    -v "$WORK/output:/work/output" \
    -v "$WORK/extra_model_paths.yaml:/work/extra_model_paths.yaml:ro" \
    "$IMAGE" bash -lc "cd /opt/ComfyUI && exec python main.py --listen 0.0.0.0 --port 8188 \
      --output-directory /work/output --extra-model-paths-config /work/extra_model_paths.yaml $FLAGS"
  echo "waiting for ComfyUI..."
  for _ in $(seq 1 180); do
    if "${PODMAN[@]}" exec "$NAME" curl -sf http://127.0.0.1:8188/system_stats >/dev/null 2>&1; then status; return; fi
    running || { logs; exit 1; }
    sleep 2
  done
  echo "timed out waiting for ComfyUI" >&2; logs; exit 1
}

status() { "${PODMAN[@]}" exec "$NAME" curl -sf http://127.0.0.1:8188/system_stats; echo; }
logs()   { "${PODMAN[@]}" logs --tail 80 "$NAME"; }
down()   { guard_replace 1 "$([[ "${1:-}" == --force ]] && echo 1 || echo 0)"; "${PODMAN[@]}" rm -f "$NAME"; }

case "${1:-}" in
  up) shift; up "$@" ;;
  down) shift; down "$@" ;;
  status) status ;;
  logs) logs ;;
  exec) shift; "${PODMAN[@]}" exec -it "$NAME" "$@" ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
