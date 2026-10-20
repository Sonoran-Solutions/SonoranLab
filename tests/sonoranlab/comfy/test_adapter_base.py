import ast
from pathlib import Path

import pytest

from sonoranlab.comfy.adapters.base import DEFAULT_BACKGROUND, GenerateRequest, RawResult

CORE = Path(__file__).resolve().parents[3] / "sonoranlab"


def test_request_and_result_round_trip(tmp_path: Path) -> None:
    req = GenerateRequest(
        track="E",
        character="samurai",
        anim="run",
        direction="north-east",
        source_image=tmp_path / "s.png",
        prompt="p",
        seed=3,
        out_dir=tmp_path / "a",
        settings={"length": 33},
    )
    assert req.background == DEFAULT_BACKGROUND
    assert GenerateRequest.from_json(req.to_json()) == req
    req.out_dir.mkdir()
    res = RawResult(request=req, status="ok", frames=[tmp_path / "a/f.png"], models={"m": "0" * 64})
    path = res.write()
    assert RawResult.from_json(__import__("json").loads(path.read_text())) == res
    with pytest.raises(FileExistsError):
        res.write()


def test_core_never_imports_comfy() -> None:
    for py in CORE.rglob("*.py"):
        if "comfy" in py.relative_to(CORE).parts:
            continue
        tree = ast.parse(py.read_text())
        # the CLI is the composition root: `generate` imports the adapter inside its handler, never at module load
        for node in tree.body if py.name == "cli.py" else ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not any(n.startswith("sonoranlab.comfy") for n in names), py
