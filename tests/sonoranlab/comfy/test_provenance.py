"""DYL-125: verified BiRefNet provenance on synthetic files (never real weights), cache, and both adapter paths."""

import csv
import json
import os
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from sonoranlab.comfy import provenance
from sonoranlab.comfy.adapters.base import RawResult

from . import test_track_c as tc
from . import test_track_e as te
from .birefnet_fixture import BIREFNET_FILES, work_untouched, write_birefnet

REPO = Path(__file__).resolve().parents[3]
WEIGHTS = "RMBG/BiRefNet/BiRefNet-general.safetensors"


@pytest.fixture
def work(tmp_path):
    work = tmp_path / "work"
    return work, write_birefnet(work)


@pytest.fixture
def counting(monkeypatch):
    hashed = []
    real = provenance._hash_file

    def count(path):
        hashed.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(provenance, "_hash_file", count)
    return hashed


def verify(work):
    root, tsv = work
    return provenance.verify_birefnet(root, models_tsv=tsv)


def test_real_workflows_and_pins_resolve_birefnet_general() -> None:
    for wf in ("workflows/video/wan22_i2v_lightx2v.json", "workflows/pose/qwen_edit_pose_native.json"):
        assert provenance.required_models(REPO / wf) == ["BiRefNet-general"]
    node = provenance.node_revision()
    assert node["revision"] == "229529e0ea63fb7085848225d74f91f6dc164956"
    assert node["repo"] == "https://github.com/1038lab/ComfyUI-RMBG"
    with open(REPO / "env/models.tsv", newline="") as f:
        rows = {r["id"]: r for r in csv.DictReader(f, delimiter="\t")}
    for row_id, _ in provenance.BIREFNET_ROWS["BiRefNet-general"]:
        assert rows[row_id]["dest"].startswith("RMBG/BiRefNet/") and len(rows[row_id]["sha256"]) == 64
    assert rows["B1"]["sha256"] == "9ab37426bf4de0567af6b5d21b16151357149139362e6e8992021b8ce356a154"


def test_resolves_and_verifies_actual_files(work) -> None:
    entry = verify(work)
    assert entry["verified"] is True and entry["reason"] is None
    assert entry["name"] == "BiRefNet-general" and entry["node"]["revision"]
    files = {f["id"]: f for f in entry["files"]}
    assert set(files) == {"B1", "B1a", "B1b", "B1c"}
    assert files["B1"]["role"] == "weights" and files["B1c"]["role"] == "config"
    for row_id, (dest, data) in BIREFNET_FILES.items():
        f = files[row_id]
        assert f["path"] == str(work[0] / dest) and f["size"] == len(data) and f["verified"]
        assert f["sha256"] == f["expected_sha256"]


def test_missing_file_refuses(work) -> None:
    (work[0] / WEIGHTS).unlink()
    entry = verify(work)
    assert entry["verified"] is False and "missing" in entry["reason"] and WEIGHTS in entry["reason"]
    assert next(f for f in entry["files"] if f["id"] == "B1")["sha256"] is None  # never invented


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode-000 files")
def test_unreadable_file_refuses(work) -> None:
    path = work[0] / WEIGHTS
    path.chmod(0)
    try:
        entry = verify(work)
    finally:
        path.chmod(0o644)
    assert entry["verified"] is False and "unreadable" in entry["reason"]


def test_mismatch_refuses(work) -> None:
    (work[0] / WEIGHTS).write_bytes(b"different weights")
    entry = verify(work)
    assert entry["verified"] is False and "!= recorded" in entry["reason"]
    weights = next(f for f in entry["files"] if f["id"] == "B1")
    assert weights["verified"] is False and weights["sha256"] != weights["expected_sha256"]


def test_rmbg_load_rewrite_verifies_but_other_edits_do_not(work) -> None:
    code = work[0] / "RMBG/BiRefNet/birefnet.py"
    original = code.read_bytes()
    code.write_bytes(original.replace(b"from .BiRefNet_config", b"from BiRefNet_config"))
    entry = verify(work)
    assert entry["verified"] is True
    assert "rewrite" in next(f for f in entry["files"] if f["id"] == "B1a")["note"]
    code.write_bytes(original + b"# edited\n")
    assert verify(work)["verified"] is False


def test_unknown_model_and_unreadable_tsv(work, tmp_path) -> None:
    entry = provenance.verify_birefnet(work[0], "BiRefNet_toonout", models_tsv=work[1])
    assert entry["verified"] is False and "no recorded files" in entry["reason"]
    entry = provenance.verify_birefnet(work[0], models_tsv=tmp_path / "absent.tsv")
    assert entry["verified"] is False and "cannot read" in entry["reason"]


def test_cache_hit_does_not_rehash(work, counting) -> None:
    assert verify(work)["verified"]
    assert sorted(counting) == sorted(Path(d).name for d, _ in BIREFNET_FILES.values())
    counting.clear()
    assert verify(work)["verified"] and counting == []
    assert (work[0] / "cache/model-sha256.json").is_file()


def test_cache_invalidates_on_content_mtime_and_inode_change(work, counting) -> None:
    path = work[0] / WEIGHTS
    verify(work)
    counting.clear()
    # same size and inode, new content, mtime_ns bumped: rehash and detect the mismatch
    st = path.stat()
    with open(path, "r+b") as f:
        f.write(b"X")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1000))
    entry = verify(work)
    assert counting == [path.name] and entry["verified"] is False
    # restore the bytes through a new inode (same size and mtime as now): the inode key forces a rehash
    counting.clear()
    replacement = path.with_suffix(".new")
    replacement.write_bytes(BIREFNET_FILES["B1"][1])
    now = path.stat()
    os.utime(replacement, ns=(now.st_atime_ns, now.st_mtime_ns))
    os.replace(replacement, path)
    assert verify(work)["verified"] is True and counting == [path.name]
    counting.clear()
    path.write_bytes(BIREFNET_FILES["B1"][1] + b"more")  # size change
    assert verify(work)["verified"] is False and counting == [path.name]


def test_corrupt_cache_is_ignored(work) -> None:
    cache = work[0] / "cache/model-sha256.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("{not json")
    assert verify(work)["verified"] is True
    assert str(work[0] / WEIGHTS) in json.loads(cache.read_text())


def test_merge_models_replaces_only_the_unknown_placeholder(work) -> None:
    prov = {"birefnet": verify(work)}
    merged = provenance.merge_models({"w.gguf": "a" * 64, "BiRefNet-general": "unknown"}, prov)
    assert "BiRefNet-general" not in merged and merged["w.gguf"] == "a" * 64
    assert merged[WEIGHTS] == prov["birefnet"]["files"][0]["expected_sha256"]
    prov["birefnet"]["verified"] = False
    assert provenance.merge_models({"BiRefNet-general": "unknown"}, prov) == {"BiRefNet-general": "unknown"}


# -- adapters: refusal before staging/submission, inventory on success ------------------------------------------


TRACKS = [
    pytest.param(te, te.TrackE, id="E"),
    pytest.param(tc, tc.TrackC, id="C"),
]


def build_env(module, tmp: Path) -> SimpleNamespace:
    """The adapter test modules' `env` fixture, built directly."""
    work = tmp / "work"
    env = SimpleNamespace(tmp=tmp, work=work, still=module.write_still(tmp / "still.png"))
    env.client, env.models_tsv = module.FakeClient(work / "output"), write_birefnet(work)
    if module is tc:
        env.pose = tc.write_pose(tmp / "pose.png")
    return env


@pytest.fixture
def make(tmp_path):
    def build(module, cls, unknown=True):
        env = build_env(module, tmp_path)
        if unknown:  # what the real client records for `"model": "BiRefNet-general"`
            run = env.client.run

            def run_with_placeholder(*a, **kw):
                rec = run(*a, **kw)
                rec.models = {**rec.models, "BiRefNet-general": "unknown"}
                return rec

            env.client.run = run_with_placeholder
        adapter = cls(env.client, work_root=env.work, workflow=object(), timeout_s=99, models_tsv=env.models_tsv)
        return env, adapter

    return build


@pytest.mark.parametrize("module,cls", TRACKS)
def test_adapter_success_records_verified_inventory(make, module, cls) -> None:
    env, adapter = make(module, cls)
    req = module.request(env)
    res = adapter.generate(req)
    assert res.status == "ok", res.error
    entry = res.model_provenance["birefnet"]
    assert entry["verified"] is True and entry["node"]["revision"] == provenance.node_revision()["revision"]
    assert {f["dest"] for f in entry["files"]} == {d for d, _ in BIREFNET_FILES.values()}
    assert "BiRefNet-general" not in res.models and res.models[WEIGHTS] == entry["files"][0]["sha256"]
    stored = json.loads((req.out_dir / "result.json").read_text())
    assert stored["model_provenance"] == res.model_provenance
    assert RawResult.from_json(stored) == res


@pytest.mark.parametrize("module,cls", TRACKS)
@pytest.mark.parametrize("problem", ["missing", "mismatch"])
def test_adapter_refuses_before_staging_or_submission(make, module, cls, problem) -> None:
    env, adapter = make(module, cls)
    weights = env.work / WEIGHTS
    weights.unlink() if problem == "missing" else weights.write_bytes(b"swapped weights")
    req = module.request(env)
    res = adapter.generate(req)
    assert res.status == "failed" and env.client.calls == []
    assert res.error["code"] == "model_provenance" and res.error["stage"] == "admission"
    assert res.error["retryable"] is False and "comfy-models" in res.error["hint"]
    assert ("missing" if problem == "missing" else "!= recorded") in res.error["message"]
    assert res.model_provenance["birefnet"]["verified"] is False
    assert {p.name for p in req.out_dir.iterdir()} == {
        "request.json",
        "result.json",
        *import_module(cls.__module__)._SUBDIRS,
    }
    assert not (env.work / "input").exists() and not (req.out_dir / "submission.json").exists()


def test_historical_result_without_inventory_still_loads(tmp_path) -> None:
    env = build_env(te, tmp_path)
    d = RawResult(request=te.request(env), status="ok", models={"BiRefNet-general": "unknown"}).to_json()
    del d["model_provenance"]  # Phase 4 results predate the field
    res = RawResult.from_json(d)
    assert res.model_provenance == {} and res.models == {"BiRefNet-general": "unknown"}
    assert work_untouched(env.work)
