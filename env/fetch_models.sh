#!/usr/bin/env bash
# Fetch every weight in env/models.tsv into $SONORANLAB_MODELS (default ~/comfy-models), pinned to the
# recorded HF revision and verified by SHA-256. Idempotent and resumable. Identical files already in
# ~/ComfyUI/models (owner's host ComfyUI) are hardlinked after a hash match, never modified.
#   bash env/fetch_models.sh            fetch/verify all rows
#   bash env/fetch_models.sh --verify   verify only (no network); non-zero exit on any mismatch/missing
#   bash env/fetch_models.sh Q1 K1 ...  only these ids
set -euo pipefail
cd "$(dirname "$0")"
MODELS="${SONORANLAB_MODELS:-$HOME/comfy-models}"
REUSE="${SONORANLAB_REUSE_DIR:-$HOME/ComfyUI/models}"
verify_only=0; [[ "${1:-}" == "--verify" ]] && { verify_only=1; shift; }
want=" $* "; fail=0

while IFS=$'\t' read -r id repo rev remote dest size sha status; do
  [[ "$id" == id ]] && continue
  [[ "$want" != "  " && "$want" != *" $id "* ]] && continue
  out="$MODELS/$dest"
  if [[ -f "$out" ]] && [[ "$(stat -c %s "$out")" == "$size" ]] && sha256sum -c --status <<<"$sha  $out"; then
    echo "ok    $id $dest"; continue
  fi
  if (( verify_only )); then echo "FAIL  $id $dest (missing or hash mismatch)"; fail=1; continue; fi
  mkdir -p "$(dirname "$out")"
  linked=0
  if [[ -d "$REUSE" ]]; then
    while IFS= read -r cand; do
      if sha256sum -c --status <<<"$sha  $cand"; then rm -f "$out"; ln "$cand" "$out"; echo "link  $id $dest <- $cand"; linked=1; break; fi
    done < <(find -L "$REUSE" -type f -size "${size}c" 2>/dev/null)
  fi
  (( linked )) && continue
  echo "get   $id $repo@${rev:0:8} $remote ($((size / 1000000)) MB)"
  curl -fL --retry 5 --retry-delay 5 -C - -o "$out.part" "https://huggingface.co/$repo/resolve/$rev/$remote"
  if [[ "$(stat -c %s "$out.part")" == "$size" ]] && sha256sum -c --status <<<"$sha  $out.part"; then
    mv "$out.part" "$out"; echo "ok    $id $dest"
  else
    echo "FAIL  $id sha256/size mismatch; kept $out.part for inspection" >&2; fail=1
  fi
done < models.tsv
exit $fail
