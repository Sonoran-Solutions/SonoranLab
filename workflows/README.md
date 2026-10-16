# ComfyUI workflows

API-format JSON only (the dict ComfyUI's `/prompt` accepts), one folder per
track. See ADR-0002.

```
workflows/<track>/
  <name>.json         API-format workflow
  params.yaml         maps logical inputs → {node_id, input_name}
  README.md           models required, flags (e.g. --force-fp32), notes from the benchmark
```

Example `params.yaml`:

```yaml
reference_image: {node: "12", input: "image"}
control_image:   {node: "27", input: "image"}
prompt:          {node: "6",  input: "text"}
seed:            {node: "3",  input: "seed"}
steps:           {node: "3",  input: "steps"}
width:           {node: "5",  input: "width"}
height:          {node: "5",  input: "height"}
output_prefix:   {node: "9",  input: "filename_prefix"}
```

To export from the ComfyUI UI: enable dev mode, "Save (API Format)". Strip
absolute paths before committing. Workflows are hashed into every job
manifest, so a change here is visible in every artifact it produced.

Planned:
- `A/qwen_edit_2511_combat_lora.json`
- `C/qwen_edit_2511_controlnet_union_pose.json`
- `E/wan22_i2v_14b_gguf_lightx2v.json`, `E/birefnet_alpha.json`
- `static/klein4b_text2img.json`, `static/klein4b_ref_edit.json`
