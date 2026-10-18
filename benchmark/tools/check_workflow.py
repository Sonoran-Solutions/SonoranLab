"""Structural check of API-format ComfyUI workflows against a ComfyUI /object_info dump (stdlib only, no GPU).

Mirrors the checks ComfyUI's own /prompt validation runs (execution.validate_inputs), without submitting anything:
  - every class_type exists; the graph has at least one output node
  - every required input is present; links are [node_id, slot] to an existing node and slot
  - link types match (same rule as execution.validate_node_input: '*' and comma-separated unions)
  - literal INT/FLOAT values are within min/max; COMBO values (model filenames included) are in the server's list
    (skipped, as ComfyUI does, for inputs a node validates itself: LoadImage's image is checked at run time)
  - unknown input names are reported as warnings (ComfyUI ignores them silently, so a typo'd patch does nothing)
  - if <name>.params.yaml sits next to <name>.json, every {node, input} it names exists and is a literal input

    python3 benchmark/tools/check_workflow.py --object-info ~/sonoranlab-work/object_info-2026-10-18.json workflows/*/*.json
    python3 benchmark/tools/check_workflow.py --url http://127.0.0.1:8189 workflows/grid/*.json   # GET /object_info only

Exit 0 when every workflow passes, 1 when any has errors.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

# ponytail: params.yaml is parsed by regex ({node: "N", input: "x"} per entry), not by a YAML parser, to stay stdlib.
# Ceiling: only the flow-mapping style used in workflows/*/*.params.yaml. Upgrade: PyYAML once the package exists.
PARAM_REF = re.compile(r'\{\s*node:\s*"([^"]+)"\s*,\s*input:\s*"([^"]+)"\s*\}')


def input_spec(spec: list) -> tuple[object, dict]:
    """(type, extra) from an object_info input entry; type is a str or a list of combo options."""
    typ = spec[0]
    extra = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
    if typ == "COMBO":
        typ = list(extra.get("options", []))
    return typ, extra


def types_match(received: str, expected: object) -> bool:
    if isinstance(expected, list):  # a combo input fed by a link (e.g. a primitive): ComfyUI accepts COMBO / '*'
        return received in ("COMBO", "*") or isinstance(received, list)
    if received == "*" or expected == "*" or received == expected:
        return True
    a, b = {t.strip() for t in str(received).split(",")}, {t.strip() for t in str(expected).split(",")}
    return bool(a & b)


def check(workflow: dict, info: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(workflow, dict) or not workflow:
        return ["workflow is not a non-empty JSON object"], warnings
    if not any(info.get(n.get("class_type"), {}).get("output_node") for n in workflow.values() if isinstance(n, dict)):
        errors.append("no output node (ComfyUI rejects the prompt)")
    for nid, node in workflow.items():
        cls = node.get("class_type") if isinstance(node, dict) else None
        if cls not in info:
            errors.append(f"{nid}: unknown class_type {cls!r}")
            continue
        schema = info[cls]["input"]
        declared = {**schema.get("optional", {}), **schema.get("required", {})}
        inputs = node.get("inputs", {})
        for name in schema.get("required", {}):
            if name not in inputs:
                errors.append(f"{nid} {cls}: required input {name!r} missing")
        for name in inputs:
            if name not in declared:
                warnings.append(f"{nid} {cls}: input {name!r} is not declared by the node (ignored by ComfyUI)")
        for name, val in inputs.items():
            if name not in declared:
                continue
            typ, extra = input_spec(declared[name])
            where = f"{nid} {cls}.{name}"
            if isinstance(val, list):
                if len(val) != 2 or not isinstance(val[1], int):
                    errors.append(f"{where}: bad link {val!r}, must be [node_id, slot]")
                    continue
                src = workflow.get(str(val[0]))
                if src is None:
                    errors.append(f"{where}: links to missing node {val[0]!r}")
                    continue
                outs = info.get(src.get("class_type"), {}).get("output")
                if outs is None:
                    continue  # unknown source class is already reported on its own node
                if not 0 <= val[1] < len(outs):
                    errors.append(f"{where}: slot {val[1]} out of range for {src['class_type']} ({len(outs)} outputs)")
                    continue
                if not types_match(outs[val[1]], typ):
                    errors.append(f"{where}: type mismatch, got {outs[val[1]]} from node {val[0]}, expects {typ}")
                continue
            if cls == "LoadImage" and name == "image":
                continue  # VALIDATE_INPUTS: any existing file under the input dir is valid; the runner provides it
            if isinstance(typ, list):
                if val not in typ:
                    shown = typ if len(typ) <= 12 else f"{len(typ)} options"
                    errors.append(f"{where}: {val!r} not in {shown}")
                continue
            if typ in ("INT", "FLOAT"):
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    errors.append(f"{where}: {val!r} is not a number")
                    continue
                if "min" in extra and val < extra["min"]:
                    errors.append(f"{where}: {val} < min {extra['min']}")
                if "max" in extra and val > extra["max"]:
                    errors.append(f"{where}: {val} > max {extra['max']}")
            elif typ == "BOOLEAN" and not isinstance(val, bool):
                errors.append(f"{where}: {val!r} is not a boolean")
            elif typ == "STRING" and not isinstance(val, str):
                errors.append(f"{where}: {val!r} is not a string")
            elif isinstance(typ, str) and typ.startswith("COMFY_"):
                warnings.append(f"{where}: V3 dynamic input type {typ} not checked")
            elif typ not in ("INT", "FLOAT", "BOOLEAN", "STRING", "COLORCODE"):
                errors.append(f"{where}: literal {val!r} given for link-only type {typ}")
    return errors, warnings


def check_params(params_text: str, workflow: dict) -> list[str]:
    errors = []
    refs = PARAM_REF.findall(params_text)
    if not refs:
        errors.append("params.yaml names no {node, input} entries")
    for nid, name in refs:
        node = workflow.get(nid)
        if node is None:
            errors.append(f"params: node {nid!r} not in workflow")
        elif name not in node.get("inputs", {}):
            errors.append(f"params: node {nid} ({node.get('class_type')}) has no input {name!r}")
        elif isinstance(node["inputs"][name], list):
            errors.append(f"params: node {nid}.{name} is a link, not a patchable value")
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--object-info", type=Path, help="saved /object_info JSON")
    src.add_argument("--url", default="http://127.0.0.1:8189", help="ComfyUI base URL (GET /object_info)")
    ap.add_argument("workflows", nargs="+", type=Path)
    args = ap.parse_args(argv)
    if args.object_info:
        info = json.loads(args.object_info.read_text())
    else:
        with urllib.request.urlopen(args.url.rstrip("/") + "/object_info", timeout=30) as r:
            info = json.load(r)
    failed = 0
    for path in args.workflows:
        if path.name.endswith(".params.yaml"):
            continue
        try:
            wf = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"FAIL {path}: {exc}")
            failed += 1
            continue
        errors, warnings = check(wf, info)
        params = path.with_suffix(".params.yaml")
        if params.exists():
            errors += check_params(params.read_text(), wf)
        status = "FAIL" if errors else "ok  "
        print(
            f"{status} {path} ({len(wf)} nodes{', params ' + params.name if params.exists() else ', no params.yaml'})"
        )
        for e in errors:
            print(f"     error: {e}")
        for w in warnings:
            print(f"     warn:  {w}")
        failed += bool(errors)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
