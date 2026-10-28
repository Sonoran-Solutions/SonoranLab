"""Verified BiRefNet provenance (DYL-125): hash the files ComfyUI-RMBG actually loads against env/models.tsv.

env/comfy.sh mounts `$WORK/RMBG` as the container's `/opt/ComfyUI/models/RMBG`, so the files the `BiRefNetRMBG` node
loads for model `BiRefNet-general` are `$WORK/RMBG/BiRefNet/{BiRefNet-general.safetensors, birefnet.py,
BiRefNet_config.py, config.json}` (rows B1, B1a-c; config.json is only existence-checked by the node, but a missing one
makes it download from the unlicensed mirror). ComfyUI-RMBG rewrites birefnet.py in place on every load
(`from .BiRefNet_config` -> `from BiRefNet_config`), so that file verifies as B1a or exactly that rewrite of B1a.

Hashes are cached in `$WORK/cache/model-sha256.json` keyed on (path, size, mtime_ns, inode): a huge weight file is
hashed once, and any change to one of those keys rehashes it. A missing, unreadable or mismatched file makes the entry
`verified: false` with a reason; nothing here ever invents a digest.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from sonoranlab.artifacts import sha256_file
from sonoranlab.comfy.guard import REPO

MODELS_TSV = REPO / "env/models.tsv"
CONTAINERFILE = REPO / "env/Containerfile"
NODE_CLASS = "BiRefNetRMBG"
NODE_PACK = "ComfyUI-RMBG"
# workflow `model` value -> models.tsv ids, as (id, role); the dest column gives the path under $WORK.
BIREFNET_ROWS = {"BiRefNet-general": (("B1", "weights"), ("B1a", "code"), ("B1b", "code"), ("B1c", "config"))}
_REWRITE = (b"from .BiRefNet_config", b"from BiRefNet_config")  # ComfyUI-RMBG AILab_BiRefNet.load_model
HINT = (
    "restore $WORK/RMBG/BiRefNet from the verified originals in ~/comfy-models/RMBG/BiRefNet "
    "(env/fetch_models.sh checks them against env/models.tsv rows B1-B1c); never edit the recorded hashes"
)

_hash_file = sha256_file  # module-level so tests can count hashing


def required_models(workflow_path: Path) -> list[str]:
    """BiRefNet `model` names used by the API-format workflow's BiRefNetRMBG nodes (sorted, unique)."""
    graph = json.loads(Path(workflow_path).read_text())
    return sorted(
        {
            str(node.get("inputs", {}).get("model"))
            for node in graph.values()
            if isinstance(node, dict) and node.get("class_type") == NODE_CLASS
        }
    )


def node_revision(containerfile: Path = CONTAINERFILE) -> dict[str, Any]:
    """The ComfyUI-RMBG pin the container image is built from (env/Containerfile `name|repo|commit`)."""
    try:
        m = re.search(rf'"{NODE_PACK}\|([^|"]+)\|([0-9a-f]{{40}})"', Path(containerfile).read_text())
    except OSError:
        m = None
    return {
        "pack": NODE_PACK,
        "repo": m.group(1) if m else None,
        "revision": m.group(2) if m else None,
        "source": "env/Containerfile",
    }


def _rows(models_tsv: Path) -> dict[str, dict[str, str]]:
    with open(models_tsv, newline="") as stream:
        return {row["id"]: row for row in csv.DictReader(stream, delimiter="\t")}


class _Cache:
    def __init__(self, path: Path | None) -> None:
        self.path, self.data, self.dirty = path, {}, False
        if path is not None:
            try:
                data = json.loads(path.read_text())
                self.data = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self.data = {}  # an absent or corrupt cache only costs a rehash

    def digest(self, path: Path, st: os.stat_result, rewritable: bool) -> dict[str, Any]:
        stat = [st.st_size, st.st_mtime_ns, st.st_ino]  # with the path: the cache key
        hit = self.data.get(str(path))
        if isinstance(hit, dict) and hit.get("stat") == stat and isinstance(hit.get("sha256"), str):
            return hit
        record: dict[str, Any] = {"stat": stat, "sha256": _hash_file(path)}
        if rewritable and st.st_size < 1 << 20:  # code file: also the digest with RMBG's import rewrite undone
            record["unrewritten_sha256"] = hashlib.sha256(path.read_bytes().replace(*_REWRITE[::-1])).hexdigest()
        after = path.stat()
        if [after.st_size, after.st_mtime_ns, after.st_ino] == stat:  # a file changed while hashing is not cached
            self.data[str(path)], self.dirty = record, True  # replaces (invalidates) any older key for this path
        return record

    def save(self) -> None:
        if self.path is None or not self.dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=self.path.parent)
            with os.fdopen(fd, "w") as stream:
                json.dump(self.data, stream, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError:
            pass  # caching is an optimisation; verification already happened


def verify_birefnet(
    work_root: Path,
    model: str = "BiRefNet-general",
    *,
    models_tsv: Path | None = None,
    cache_path: Path | None = None,
    containerfile: Path = CONTAINERFILE,
) -> dict[str, Any]:
    """Structured inventory entry for one BiRefNet model: files (path, sha256, size, role), node revision,
    `verified` and the first `reason` it is not. `cache_path` defaults to `<work_root>/cache/model-sha256.json`."""
    work_root = Path(work_root)
    entry: dict[str, Any] = {
        "name": model,
        "node": node_revision(containerfile),
        "root": str(work_root / "RMBG"),
        "files": [],
        "verified": False,
        "reason": None,
    }
    if model not in BIREFNET_ROWS:
        entry["reason"] = f"model {model!r} has no recorded files in env/models.tsv (known: {sorted(BIREFNET_ROWS)})"
        return entry
    models_tsv = Path(models_tsv) if models_tsv is not None else MODELS_TSV
    try:
        rows = _rows(models_tsv)
    except (OSError, KeyError, csv.Error) as exc:
        entry["reason"] = f"cannot read {models_tsv}: {exc}"
        return entry
    cache = _Cache(cache_path if cache_path is not None else work_root / "cache/model-sha256.json")
    problems = []
    for row_id, role in BIREFNET_ROWS[model]:
        row = rows.get(row_id)
        if row is None or not re.fullmatch(r"[0-9a-f]{64}", row.get("sha256") or ""):
            problems.append(f"env/models.tsv has no sha256 for row {row_id}")
            continue
        path = work_root / row["dest"]
        item: dict[str, Any] = {"id": row_id, "role": role, "path": str(path), "dest": row["dest"]}
        item.update({"expected_sha256": row["sha256"], "sha256": None, "size": None, "verified": False})
        entry["files"].append(item)
        try:
            st = path.stat()
            item["size"] = st.st_size
            record = cache.digest(path, st, rewritable=role == "code" and path.suffix == ".py")
        except FileNotFoundError:
            problems.append(f"{row['dest']}: missing at {path}")
            continue
        except OSError as exc:
            problems.append(f"{row['dest']}: unreadable ({exc.strerror or exc})")
            continue
        item["sha256"] = record["sha256"]
        if record["sha256"] == row["sha256"]:
            item["verified"] = True
        elif record.get("unrewritten_sha256") == row["sha256"]:
            item["verified"], item["note"] = True, "ComfyUI-RMBG load-time import rewrite of the recorded file"
        else:
            problems.append(f"{row['dest']}: sha256 {record['sha256'][:12]} != recorded {row['sha256'][:12]}")
    cache.save()
    entry["verified"] = not problems and bool(entry["files"])
    entry["reason"] = "; ".join(problems) or None
    return entry


def verify_workflow(work_root: Path, workflow_path: Path, **kw: Any) -> dict[str, dict[str, Any]]:
    """`{"birefnet": entry}` for the workflow's BiRefNet model ({} when it has no BiRefNetRMBG node). More than one
    distinct BiRefNet model in one workflow is refused: the inventory records exactly one."""
    names = required_models(workflow_path)
    if not names:
        return {}
    if len(names) > 1:
        entry = verify_birefnet(work_root, names[0], **kw)
        entry.update(verified=False, reason=f"workflow uses several BiRefNet models: {names}")
        return {"birefnet": entry}
    return {"birefnet": verify_birefnet(work_root, names[0], **kw)}


def admit(
    work_root: Path, workflow_path: Path, *, models_tsv: Path | None = None
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Admission check shared by the adapters and the job layer: (provenance, refusal message or None)."""
    try:
        prov = verify_workflow(work_root, workflow_path, models_tsv=models_tsv)
    except (OSError, ValueError, AttributeError) as exc:
        return {}, f"model provenance: cannot read workflow {workflow_path}: {exc}"
    return prov, unverified(prov)


def unverified(provenance: dict[str, dict[str, Any]]) -> str | None:
    """A refusal message naming every unverified model, or None when all verified."""
    bad = [f"{entry['name']}: {entry['reason']}" for entry in provenance.values() if not entry.get("verified")]
    return f"model provenance not verified: {'; '.join(bad)}" if bad else None


def merge_models(models: dict[str, str], provenance: dict[str, dict[str, Any]]) -> dict[str, str]:
    """The client's `models` map (models.tsv dest -> sha256) with its `"<model>": "unknown"` placeholder replaced by
    the verified files' recorded hashes. Unverified entries stay exactly as the client recorded them."""
    result = dict(models)
    for entry in provenance.values():
        if entry.get("verified") and result.get(entry["name"]) == "unknown":
            del result[entry["name"]]
            result.update({f["dest"]: f["expected_sha256"] for f in entry["files"]})
    return result
