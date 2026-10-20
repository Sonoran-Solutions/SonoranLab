"""sonoranlab.artifacts: job dir, raw copies, stage writes, manifest, verify_raw (synthetic)."""

import json
import os
import re
import stat

import numpy as np
import pytest

from sonoranlab import artifacts as art


@pytest.fixture
def src(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    (d / "a.png").write_bytes(b"\x89PNG-a" * 100)
    (d / "selection.json").write_text('{"seed": 7}')
    os.utime(d / "a.png", (1_000_000_000, 1_000_000_000))
    return d


@pytest.fixture
def profile(tmp_path):
    p = tmp_path / "profile.yaml"
    p.write_text("id: toy\n")
    return p


@pytest.fixture
def job(tmp_path):
    return art.create_job(tmp_path / "job")


def test_create_job_makes_dirs_and_accepts_an_empty_dir(tmp_path):
    (tmp_path / "empty").mkdir()
    j = art.create_job(tmp_path / "empty")
    assert (j / "raw").is_dir() and (j / "processed").is_dir()
    assert art.create_job(tmp_path / "deep" / "new") == tmp_path / "deep" / "new"


def test_create_job_refuses_non_empty_dir_and_files(tmp_path, job):
    with pytest.raises(FileExistsError, match="nothing is overwritten") as e:
        art.create_job(job)  # now holds raw/ and processed/
    assert str(job) in str(e.value)
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(FileExistsError):
        art.create_job(f)


def test_ingest_raw_copies_bytes_read_only_and_leaves_sources_alone(src, job):
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns, stat.S_IMODE(p.stat().st_mode)) for p in src.iterdir()}
    hashes = art.ingest_raw(job, [src / "a.png", src / "selection.json"])
    assert set(hashes) == {"a.png", "selection.json"}
    for name, h in hashes.items():
        assert (job / "raw" / name).read_bytes() == (src / name).read_bytes()
        assert h == art.sha256_file(src / name) == art.sha256_file(job / "raw" / name)
        assert stat.S_IMODE((job / "raw" / name).stat().st_mode) == 0o444
    assert {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns, stat.S_IMODE(p.stat().st_mode)) for p in src.iterdir()
    } == before


def test_ingest_raw_dict_names_and_refusals(src, job, tmp_path):
    assert set(art.ingest_raw(job, {"north_0.png": src / "a.png"})) == {"north_0.png"}
    with pytest.raises(FileExistsError, match="once per job"):  # second ingest
        art.ingest_raw(job, [src / "selection.json"])
    other = art.create_job(tmp_path / "job2")
    with pytest.raises(ValueError, match="plain file name"):
        art.ingest_raw(other, {"../x.png": src / "a.png"})
    with pytest.raises(ValueError, match="share a name"):
        art.ingest_raw(other, [src / "a.png", src / "a.png"])
    with pytest.raises(FileNotFoundError):
        art.ingest_raw(other, [src / "missing.png"])
    assert not any((other / "raw").iterdir())  # refusals copy nothing


def test_ingest_raw_refuses_a_source_inside_the_job(src, job):
    inside = job / "processed" / "x.png"
    inside.write_bytes(b"x")
    with pytest.raises(ValueError, match="inside the job"):
        art.ingest_raw(job, [src / "a.png", inside])
    assert not any((job / "raw").iterdir())


def clips():
    f = np.zeros((4, 4, 4), np.uint8)
    f[1, 1] = (10, 20, 30, 255)
    return {"south": [f, f.copy()], "east": [f.copy()]}


def test_write_stage_paths_hashes_and_refusals(job):
    hashes = art.write_stage(job, "02_pixelize", clips())
    assert sorted(hashes) == [f"processed/02_pixelize/{n}.png" for n in ("east_0", "south_0", "south_1")]
    assert all(hashes[k] == art.sha256_file(job / k) for k in hashes)
    with pytest.raises(FileExistsError):
        art.write_stage(job, "02_pixelize", clips())
    for bad in ("pixelize", "2_pixelize", "02_Pixelize", "02_a/b", "../01_x"):
        with pytest.raises(ValueError, match="must match"):
            art.write_stage(job, bad, clips())
    with pytest.raises(ValueError, match="RGBA"):
        art.write_stage(job, "03_bad", {"south": [np.zeros((4, 4, 3), np.uint8)]})


def test_write_stage_is_deterministic(tmp_path):
    a, b = art.create_job(tmp_path / "a"), art.create_job(tmp_path / "b")
    assert art.write_stage(a, "01_alpha", clips()) == art.write_stage(b, "01_alpha", clips())


def test_write_json_format_and_refuses_overwrite(job):
    p = art.write_json(job, "report.json", {"b": 1, "a": [1, 2]})
    assert p.read_text() == '{\n "a": [\n  1,\n  2\n ],\n "b": 1\n}\n'
    with pytest.raises(FileExistsError):
        art.write_json(job, "report.json", {})
    with pytest.raises(ValueError):
        art.write_json(job, "sub/report.json", {})


def manifest(job, profile, inputs, **kw):
    args = dict(
        job=job,
        profile_path=profile,
        character="toy",
        animation="walk",
        inputs=inputs,
        outputs={"processed/01_alpha/south_0.png": "ab" * 32},
        params={"scale": 3},
        timings={"total_s": 1.5},
        upstream={"seed": 7, "model": "m"},
    )
    return art.build_manifest(**{**args, **kw})


def test_manifest_keys_and_json_serializable(job, profile, src):
    m = manifest(job, profile, art.ingest_raw(job, [src / "a.png"]))
    assert m["schema"] == "sonoranlab.manifest/1"
    assert set(m) == {
        "schema", "profile", "character", "animation", "inputs", "outputs", "params", "tools", "git", "upstream",
        "timings", "created_at",
    }  # fmt: skip
    assert m["profile"] == {"path": profile.as_posix(), "sha256": art.sha256_file(profile)}
    assert {"python", "numpy", "pillow", "pyyaml", "pixeloe", "torch"} <= set(m["tools"])
    assert m["tools"]["numpy"] == np.__version__ and m["tools"]["pixeloe"] == "1.0.0"
    assert m["git"] is None or re.fullmatch(r"[0-9a-f]{40}", m["git"])
    assert m["upstream"] == {"seed": 7, "model": "m"}
    assert json.loads(json.dumps(m)) == m
    assert manifest(job, profile, {}, upstream=None)["upstream"] is None


def test_manifests_for_identical_inputs_differ_only_in_created_at_and_timings(tmp_path, profile, src):
    ms = []
    for n, t in (("j1", 1.0), ("j2", 2.0)):
        j = art.create_job(tmp_path / n)
        ms.append(manifest(j, profile, art.ingest_raw(j, [src / "a.png"]), timings={"total_s": t}))
    drop = lambda m: {k: v for k, v in m.items() if k not in {"created_at", "timings"}}  # noqa: E731
    assert drop(ms[0]) == drop(ms[1])
    assert ms[0]["timings"] != ms[1]["timings"]


def test_manifest_requires_a_job_dir(tmp_path, profile):
    with pytest.raises(FileNotFoundError):
        manifest(tmp_path / "nope", profile, {})


def test_verify_raw_detects_tamper_missing_and_extra(job, profile, src):
    inputs = art.ingest_raw(job, [src / "a.png", src / "selection.json"])
    art.write_json(job, "manifest.json", manifest(job, profile, inputs))
    assert art.verify_raw(job) == []
    raw = job / "raw" / "a.png"
    raw.chmod(0o644)
    raw.write_bytes(raw.read_bytes() + b"!")
    (job / "raw" / "extra.png").write_bytes(b"x")
    (job / "raw" / "selection.json").chmod(0o644)
    (job / "raw" / "selection.json").unlink()
    problems = art.verify_raw(job)
    assert len(problems) == 3
    assert any("a.png: sha256" in p for p in problems)
    assert "raw/selection.json: missing" in problems
    assert "raw/extra.png: not in manifest" in problems
