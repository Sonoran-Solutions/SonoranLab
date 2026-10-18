#!/usr/bin/env bash
# SonoranLab ComfyUI container (kyuz0 Strix Halo toolbox, pinned by digest).
# Runs alongside the owner's host ComfyUI (:8188) without touching it.
#   env/comfy.sh up            start on 127.0.0.1:8189
#   env/comfy.sh up --offline  start with --network=none (DYL-69 offline test; API reachable only via `exec`)
#   env/comfy.sh down | status | logs | exec <cmd...>
set -euo pipefail

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

up() {
  local net=(-p "127.0.0.1:${PORT}:8188")
  [[ "${1:-}" == "--offline" ]] && net=(--network=none)
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
  podman image exists "$IMAGE" || podman build -t "$IMAGE" -f "$HERE/Containerfile" "$HERE"
  # ComfyUI-RMBG rewrites birefnet.py in place on load, so it gets a writable copy; the verified originals stay untouched.
  mkdir -p "$MODELS/RMBG" "$MODELS/controlnet_aux_ckpts" "$WORK/RMBG"
  cp -a -u "$MODELS/RMBG/." "$WORK/RMBG/"
  podman rm -f "$NAME" >/dev/null 2>&1 || true
  # Own systemd scope for conmon/port forwarder, so killing the launching terminal's scope (e.g. an oomd kill of
  # the app that hosts the terminal) doesn't orphan the container.
  local scope=(); command -v systemd-run >/dev/null && scope=(systemd-run --user --scope --quiet --collect)
  "${scope[@]}" podman run -d --name "$NAME" "${net[@]}" \
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
    if podman exec "$NAME" curl -sf http://127.0.0.1:8188/system_stats >/dev/null 2>&1; then status; return; fi
    podman container exists "$NAME" && [[ "$(podman inspect -f '{{.State.Running}}' "$NAME")" == true ]] || { logs; exit 1; }
    sleep 2
  done
  echo "timed out waiting for ComfyUI" >&2; logs; exit 1
}

status() { podman exec "$NAME" curl -sf http://127.0.0.1:8188/system_stats; echo; }
logs()   { podman logs --tail 80 "$NAME"; }
down()   { podman rm -f "$NAME"; }

case "${1:-}" in
  up) shift; up "$@" ;;
  down) down ;;
  status) status ;;
  logs) logs ;;
  exec) shift; podman exec -it "$NAME" "$@" ;;
  *) sed -n '2,7p' "$0"; exit 2 ;;
esac
