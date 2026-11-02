# DYL-125: verified BiRefNet weights and model provenance (2026-10-28)

Finding F7 (Phase 4 gate): Track E attempts recorded `"BiRefNet-general": "unknown"`. Cause: `ComfyClient._models`
matches a workflow model value against models.tsv by filename suffix, and the node's `model` input is the bare name
`BiRefNet-general`, so no row matched. Alpha weights shape edges and silhouettes, so this is a reproducibility gap.

## What the node loads (traced, read-only)

- Workflows: `workflows/video/wan22_i2v_lightx2v.json` (Track E, node 92 input) and
  `workflows/pose/qwen_edit_pose_native.json` (Track C) both use `class_type: BiRefNetRMBG`, `model: BiRefNet-general`.
- Node pin: `ComfyUI-RMBG` `https://github.com/1038lab/ComfyUI-RMBG` @ `229529e0ea63fb7085848225d74f91f6dc164956`
  (env/Containerfile). The image layer checkout under podman storage has `.git/HEAD` = the same commit,
  `pyproject.toml` version 3.2.0, GPL-3.0 LICENSE. `py/AILab_BiRefNet.py` sha256 `8680425a77b1739ce89eb298d5fdf3d833ce99e75846f61b5f36ff978386b999`.
- `AILab_BiRefNet.py` `MODEL_CONFIG["BiRefNet-general"]`: cache dir `models/RMBG/BiRefNet`, files `birefnet.py`,
  `BiRefNet_config.py`, `BiRefNet-general.safetensors`, `config.json`. `check_model_cache` requires all four
  (else `hf_hub_download` from the unlicensed `1038lab/BiRefNet` mirror). `load_model` reads and **rewrites
  `birefnet.py` in place** (`from .BiRefNet_config` -> `from BiRefNet_config`), execs `BiRefNet_config.py` and
  `birefnet.py` via importlib, then `safetensors.load_file` on the weights. `config.json` is existence-checked only.
- Mount (env/comfy.sh): `$WORK/RMBG` (`~/sonoranlab-work/RMBG`) -> `/opt/ComfyUI/models/RMBG`, a writable copy made
  with `cp -a -u` from `$MODELS/RMBG` (`~/comfy-models/RMBG`, verified originals). So the files actually loaded are
  `~/sonoranlab-work/RMBG/BiRefNet/*`.

## Hashes (once, offline)

Command: `cd ~ && stat -c '%n %s %y %i' <files>; sha256sum comfy-models/RMBG/BiRefNet/* sonoranlab-work/RMBG/BiRefNet/{birefnet.py,BiRefNet_config.py,config.json,BiRefNet-general.safetensors}` (0.63 s wall).

| file (under `~/sonoranlab-work/RMBG/BiRefNet/`, loaded) | kind | bytes | mtime | sha256 | models.tsv |
|---|---|---|---|---|---|
| BiRefNet-general.safetensors | weights | 444473596 | 2026-10-18 13:05:12.692 -0700 | `9ab37426bf4de0567af6b5d21b16151357149139362e6e8992021b8ce356a154` | B1, match |
| birefnet.py | code (RMBG-rewritten) | 91895 | 2026-10-20 21:15:26.758 -0700 | `c7565b81cd3adc215c113a2816cf6c92804908b34a36d4a013e5176761b30455` | B1a after the one-line import rewrite (diff confirms that is the only change) |
| BiRefNet_config.py | code | 298 | 2026-10-18 13:05:13.333 -0700 | `e7b8c2a74f6cea6a59553d517f71d47f2c1d90e670a13416af17c25fe2f3dc52` | B1b, match |
| config.json | config | 405 | 2026-10-18 13:05:13.502 -0700 | `c97ea21569daf66b205491a4635147dd3bc42c7c168b89d7d75b53f67ef548ae` | B1c, match |

Originals in `~/comfy-models/RMBG/BiRefNet/` hash to the B1, B1a (`208771ae…5ef`, 91896 bytes), B1b, B1c rows exactly.
Upstream revision/license come only from existing records: models.tsv rows B1-B1c (`ZhengPeng7/BiRefNet` @
`e2bf8e4460fc8fa32bba5ea4d94b3233d367b0e4`) and docs/04 (MIT). The directory contains no README/LICENSE of its own.
models.tsv already had all four rows with these hashes; no rows were added or changed.

## Implementation

- `sonoranlab/comfy/provenance.py`: resolves the workflow's BiRefNetRMBG model to rows B1-B1c, hashes
  `$WORK/<dest>` against models.tsv, accepts `birefnet.py` as B1a or exactly RMBG's rewrite of it, records node pin
  from env/Containerfile. Cache `$WORK/cache/model-sha256.json`, keyed on (path, size, mtime_ns, inode); any key
  change rehashes; a file changing while hashed is not cached. Real host check with a scratch cache:
  first run 0.221 s, second 0.000 s (cache hit), `verified: true`.
- Track E / Track C adapters: verify after writing request.json and before staging or submission; result carries
  `model_provenance: {"birefnet": {name, node, root, files[{id, role, path, dest, sha256, expected_sha256, size,
  verified, note?}], verified, reason}}`; `models` has the `"BiRefNet-general": "unknown"` placeholder replaced by the
  verified rows' hashes.
- Job layer (`sonoranlab/jobs/kinds/generation.py`): admission (`animate`, `pose_frame`) refuses with
  `refused` + hint; a queued job whose files changed fails at run start with `model_provenance` (stage `prepare`)
  before any child starts. Attempts carry `model_provenance`; `job_status`/`job_result` summaries (CLI `sonoranlab
  api` and MCP share them) include it per attempt and in `raw_result`.

## Historical Phase 4 job

`job-20261021T040422Z-1bafb051` stays `unknown`; its manifests and results are untouched. The current loaded files
verify, and the weights file's mtime/inode (2026-10-18) predate that job, which is consistent with the same bytes but
does not prove it: nothing hashed them at run time. The run-time `birefnet.py` rewrite (mtime 2026-10-20 21:15 PDT,
during the job) shows RMBG loaded from this directory.
