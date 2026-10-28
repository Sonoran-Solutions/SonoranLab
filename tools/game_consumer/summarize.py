"""tools/game_consumer result helper (no game code, no third-party imports; run with `python3 -I`).

  summarize.py pre BUNDLE SNAPSHOT OUT       -> OUT/pre.json: bundle files' sha256 in the snapshot, HEAD, git status
  summarize.py final BUNDLE SNAPSHOT OUT     -> OUT/result.json (`sonoranlab.game_consumer_result/1`); exit 0 iff pass
"""

import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SCHEMA = "sonoranlab.game_consumer_result/1"
GALLERY_BUDGET = 128 << 20


def sha(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else None


def git(snap: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(snap), *args], capture_output=True, text=True, timeout=60).stdout


def snapshot(bundle: dict, snap: Path) -> dict:
    return {
        "files_sha256": {f["path"]: sha(snap / f["path"]) for f in bundle["files"]},
        "head": git(snap, "rev-parse", "HEAD").strip(),
        "status": sorted(e for e in git(snap, "status", "--porcelain=v1", "--untracked-files=all", "-z").split("\0") if e),
    }


def step(out: Path, name: str) -> dict:
    p = out / "logs" / f"{name}.status"
    if not p.is_file():
        return {"ran": False}
    rec = json.loads(p.read_text())
    rec["ran"] = True
    rec["pass"] = rec.get("exit") == 0 and not rec.get("timed_out") and not rec.get("flooded")
    return rec


def log_text(out: Path, name: str) -> str:
    return "".join(
        p.read_text(errors="replace") for p in (out / "logs" / f"{name}.head.log", out / "logs" / f"{name}.tail.log")
        if p.is_file()
    )


def pixel_check_verdict(out: Path, bundle: dict, status: dict) -> dict:
    """pass; known_missing_clip (exactly "<id>: clip table" for a sheet without death); fail otherwise."""
    cid = bundle["character"]
    text = log_text(out, "pixel_check")
    no_death = any(m.get("name") == "death" for m in bundle.get("known_missing_clips", []))
    if status.get("pass"):
        return {"verdict": "pass", "message": "exit 0"}
    if status.get("exit") == 1 and no_death and f"InvalidOperationException: {cid}: clip table" in text:
        return {
            "verdict": "known_missing_clip",
            "message": f"{cid}: clip table (no death clip); PixelUnitChecks.Check did not run after it",
        }
    lines = [ln for ln in text.splitlines() if "Exception" in ln or "ERROR" in ln][:5]
    return {"verdict": "fail", "message": f"status {status}; {lines}"}


def trx_counts(out: Path) -> dict:
    files = sorted((out / "dotnet").glob("*.trx"))
    if not files:
        return {"executed": 0, "passed": 0, "failed": 0, "error": "no .trx result"}
    counters = ET.parse(files[-1]).getroot().find(".//{*}ResultSummary/{*}Counters")
    if counters is None:
        return {"executed": 0, "passed": 0, "failed": 0, "error": "no Counters in .trx"}
    return {k: int(counters.get(k, "0")) for k in ("total", "executed", "passed", "failed")}


def main() -> int:
    mode, bundle_dir, snap, out = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
    bundle = json.loads((bundle_dir / "bundle.json").read_text())
    if mode == "pre":
        (out / "pre.json").write_text(json.dumps(snapshot(bundle, snap), indent=1))
        return 0
    pre = json.loads((out / "pre.json").read_text())
    post = snapshot(bundle, snap)
    want = {f["path"]: f["sha256_after"] for f in bundle["files"]}
    expected_status = sorted(("?? " if f["action"] == "add" else " M ") + f["path"] for f in bundle["files"])
    failures = []
    if pre["files_sha256"] != want:
        failures.append("the snapshot does not hold the bundle's files (apply the bundle first)")
    if pre["head"] != bundle["target"]["revision"]:
        failures.append(f"snapshot HEAD {pre['head']} is not the bundle's target revision")
    rewritten = sorted(p for p in want if post["files_sha256"].get(p) != pre["files_sha256"].get(p))
    if rewritten:
        failures.append(f"the engine rewrote bundle files {rewritten} (copies in OUT/rewritten/)")
    if post["status"] != expected_status:
        failures.append(f"snapshot status after the run {post['status']} != the bundle's {expected_status}")
    steps = {n: step(out, n) for n in ("dotnet_test", "game_build", "godot_import", "pixel_check", "gallery_capture")}
    tests = trx_counts(out)
    steps["dotnet_test"]["counts"] = tests
    if not steps["dotnet_test"].get("pass") or tests.get("executed", 0) < 1 or tests.get("failed", 0):
        failures.append(f"game tests did not pass with >= 1 executed test: {tests}")
    for n in ("game_build", "godot_import"):
        if not steps[n].get("pass"):
            failures.append(f"{n} failed: {steps[n]}")
    pixel_check = pixel_check_verdict(out, bundle, steps["pixel_check"])
    if pixel_check["verdict"] == "fail":
        failures.append(f"game --pixel-check failed: {pixel_check['message']}")
    gallery = sorted((out / "gallery").glob("*.png"))
    gallery_bytes = sum(g.stat().st_size for g in gallery)
    gallery_done = "gallery captures complete" in log_text(out, "gallery_capture")
    if not steps["gallery_capture"].get("pass") or not gallery or not gallery_done:
        failures.append(f"game --pixel-gallery-capture did not complete: {steps['gallery_capture']}")
    if gallery_bytes > GALLERY_BUDGET:
        failures.append(f"gallery captures use {gallery_bytes} bytes > budget {GALLERY_BUDGET}")
    fallback = out / "fallback" / "godot_result.json"
    result = {
        "schema": SCHEMA,
        "pass": not failures,
        "character": bundle["character"],
        "snapshot": str(snap.resolve()),
        "target_head": pre["head"],
        "files_sha256": pre["files_sha256"],
        "files_sha256_after_run": post["files_sha256"],
        "engine_rewrote": rewritten,
        "snapshot_status_after": post["status"],
        "steps": steps,
        "game_pixel_check": pixel_check,
        "gallery": {"captures": len(gallery), "bytes": gallery_bytes, "dir": str(out / "gallery"),
                    "note": "PixelGallery's fixed Layout: proves the patched manifest keeps the real gallery working; "
                            "the new id is not in Layout and is not drawn there"},
        "fallback_render_non_consumer": json.loads(fallback.read_text()) if fallback.is_file() else None,
        "failures": failures,
    }
    (out / "result.json").write_text(json.dumps(result, indent=1))
    print(json.dumps({"pass": result["pass"], "failures": failures}, indent=1))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
