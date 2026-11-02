"""Synthetic BiRefNet files + models.tsv for provenance tests (DYL-125); never real weights."""

import hashlib

# Stand-ins for the files ComfyUI-RMBG loads from $WORK/RMBG/BiRefNet.
BIREFNET_FILES = {
    "B1": ("RMBG/BiRefNet/BiRefNet-general.safetensors", b"synthetic weights\x00" * 64),
    "B1a": ("RMBG/BiRefNet/birefnet.py", b"import torch\nfrom .BiRefNet_config import BiRefNetConfig\n"),
    "B1b": ("RMBG/BiRefNet/BiRefNet_config.py", b"class BiRefNetConfig:\n    pass\n"),
    "B1c": ("RMBG/BiRefNet/config.json", b'{"architectures": ["BiRefNet"]}\n'),
}


def write_birefnet(work, tsv=None):
    """Write the synthetic BiRefNet files under `work` and a models.tsv recording their real sha256; returns the tsv."""
    rows = ["id\trepo\trevision\tremote_path\tdest\tsize_bytes\tsha256\tstatus"]
    for row_id, (dest, data) in BIREFNET_FILES.items():
        path = work / dest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        rows.append(f"{row_id}\tZhengPeng7/BiRefNet\t{'0' * 40}\t{path.name}\t{dest}\t{len(data)}\t{digest}\tdefault")
    tsv = tsv or work.parent / "models.tsv"
    tsv.write_text("\n".join(rows) + "\n")
    return tsv


def work_untouched(work):
    """Nothing but the synthetic BiRefNet files exists under `work` (no staging, outputs or hash cache)."""
    files = sorted(p.relative_to(work).as_posix() for p in work.rglob("*") if not p.is_dir())
    return files == sorted(dest for dest, _ in BIREFNET_FILES.values())
