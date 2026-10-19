import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_workflow", ROOT / "benchmark/tools/check_workflow.py")
cw = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cw)

INFO = {
    "UNETLoader": {"input": {"required": {"unet_name": [["a.safetensors"], {}]}}, "output": ["MODEL"]},
    "LoraLoaderModelOnly": {
        "input": {
            "required": {
                "model": ["MODEL"],
                "lora_name": ["COMBO", {"options": ["l.safetensors"]}],
                "strength_model": ["FLOAT", {"min": -100.0, "max": 100.0}],
            }
        },
        "output": ["MODEL"],
    },
    "LoadImage": {"input": {"required": {"image": [["example.png"], {}]}}, "output": ["IMAGE", "MASK"]},
    "SaveImage": {
        "input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING", {}]}},
        "output": [],
        "output_node": True,
    },
}
GOOD = {
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "a.safetensors"}},
    "2": {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {"model": ["1", 0], "lora_name": "l.safetensors", "strength_model": 1},
    },
    "3": {"class_type": "LoadImage", "inputs": {"image": "sonoranlab/runtime.png"}},
    "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0], "filename_prefix": "p1/x"}},
}


def errors(wf):
    return cw.check(wf, INFO)[0]


def test_good_graph_passes_and_loadimage_is_runtime_checked():
    assert errors(GOOD) == []


def test_catches_missing_model_wrong_link_type_range_and_missing_input():
    bad = {k: {"class_type": v["class_type"], "inputs": dict(v["inputs"])} for k, v in GOOD.items()}
    bad["1"]["inputs"]["unet_name"] = "missing.safetensors"
    bad["2"]["inputs"]["strength_model"] = 1000
    bad["4"]["inputs"]["images"] = ["1", 0]  # MODEL into IMAGE
    del bad["4"]["inputs"]["filename_prefix"]
    bad["5"] = {"class_type": "Nope", "inputs": {}}
    msgs = "\n".join(errors(bad))
    for needle in ("not in", "> max", "type mismatch", "filename_prefix", "unknown class_type"):
        assert needle in msgs


def test_params_refs_must_name_literal_inputs():
    text = 'a: {node: "2", input: "strength_model"}\nb: {node: "2", input: "model"}\nc: {node: "9", input: "x"}\n'
    msgs = cw.check_params(text, GOOD)
    assert len(msgs) == 2 and "link" in msgs[0] and "not in workflow" in msgs[1]
