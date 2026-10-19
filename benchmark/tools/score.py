"""Phase 1 scorer: one benchmark/scoring.csv row per ATTEMPTED (track, character, seed[, variant]) set.

Every set with at least one manifest.json gets a row, including sets whose attempts all failed. Which attempts count
comes from SET/selection.json (schema sonoranlab.selection/1, written by assemble_set.py), never from directory order:
the seed dir's `set/` (B: the variant dir's `set/`). SET/processed/report.json is used only if its
provenance.selection_sha256 equals the sha256 of that selection.json. A set without selection.json is "not assembled":
no validator verdict, no processed hash.

gen_minutes           sum of wall_s of the SELECTED attempts ("" if none is selected/successful)
attempt_minutes_total sum over ALL attempts, failed ones included
usable                frozen rubric: "yes" only if human_usable == yes AND validator_pass == yes; "no" if either is
                      no; "" if unknown. Only a BOUND review counts as the human verdict: a stale review (of a different
                      artifact) or an unbound one (no artifact hash) is ignored here, so it can never make usable "yes"
                      (human_usable still shows what it said).
review_binding        bound (entry's artifact_sha256 == processed_sha256), stale (differs), unbound (entry has no
                      hash), "" (no review entry)
Empty fields mean "not measured"; 0 is only written for a measured zero. peak_mem_gb = largest host MemAvailable
drop across the selected (or, not assembled, successful) attempts.

    uv run --no-project python -I benchmark/tools/score.py RESULTS_DATE_DIR OUT.csv [--review review.json]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path

COLS = ["date", "track", "character", "seed", "variant", "attempts", "failed_attempts", "gen_minutes",
        "attempt_minutes_total", "process_seconds", "cleanup_minutes", "human_usable", "validator_pass", "usable",
        "frames_fixed", "back_row_accessories_kept", "foot_slide_max_px", "off_palette_before", "peak_mem_gb",
        "processed_sha256", "review_binding", "notes"]
CHAR_NO = {"samurai": 1, "oathguard": 2, "relay_sentinel": 3}
DIRS = {"north", "north-east", "east", "south-east", "south"}


def set_key(job: str) -> tuple | None:
    """Job id -> (track, char, seed, variant) of the scored set, or None for jobs outside the sweep protocol.

    Retries end in -r1..-r6 and fold into their set. A: the samurai seed-1 sample is A/samurai/1/smoke-try2 (same
    settings as the sweep). B: one set per checkpoint. C: only the 1024-padded batches. E: only the 5 direction clips."""
    parts = [re.sub(r"-r[1-6]$", "", x) for x in job.split("/")[1:]]
    track, char, seed, rest = parts[0], parts[1], parts[2], parts[3:]
    if track == "A":
        return (track, char, seed, "") if not rest or rest == ["smoke-try2"] else None
    if track == "B":
        return (track, char, seed, rest[0]) if rest and rest[0].startswith("ck") else None
    if track == "C":
        return (track, char, seed, "") if rest and rest[0].endswith("-r1024") and rest[0][:-6] in DIRS else None
    if track == "E":
        return (track, char, seed, "") if rest and rest[0] in DIRS else None
    return None


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def set_dir(date_dir: Path, track: str, char: str, seed: str, variant: str) -> Path:
    """<date>/<track>/<char>/<seed>/set, for B <date>/B/<char>/<seed>/<variant>/set."""
    return date_dir / track / char / seed / (variant if track == "B" else "") / "set"


def minutes(vals: list[float | None]) -> tuple[float | str, int]:
    """(sum of the measured wall times in minutes or "", how many attempts had no wall time)."""
    got = [v for v in vals if v is not None]
    return (round(sum(got) / 60, 1) if got else ""), len(vals) - len(got)


def combine(human: str, validator: str) -> str:
    if "no" in (human, validator):
        return "no"
    return "yes" if human == validator == "yes" else ""


def row_for(date_dir: Path, key: tuple, mans: list[dict], review: dict) -> dict:
    track, char, seed, variant = key
    # attempts by job id: manifests first, then selection.json (the source of truth) overrides/extends them
    att: dict[str, dict] = {}
    for m in mans:
        att[m["job"]] = {"ok": m.get("exit_code") == 0, "wall_s": (m.get("run") or {}).get("wall_s"), "man": m}
    sdir = set_dir(date_dir, *key)
    sel_path = sdir / "selection.json"
    sel = json.loads(sel_path.read_text()) if sel_path.exists() else None
    notes = [variant] if variant else []
    if sel:
        for a in sel.get("attempts", []):
            cur = att.setdefault(a["job"], {"man": None, "wall_s": None})
            cur["ok"] = a.get("exit_code") == 0
            cur["wall_s"] = a.get("wall_s", cur["wall_s"])
            cur["selected"] = bool(a.get("selected"))
        chosen = [a for a in att.values() if a.get("selected")]
        if sel.get("missing_parts"):
            notes.append(f"missing parts: {sorted(sel['missing_parts'])}")
    else:
        chosen = [a for a in att.values() if a["ok"]]
        notes.append("not assembled (no selection.json): gen_minutes counts every successful attempt")
        if track in "CE":
            got = {re.sub(r"-r1024$", "", re.sub(r"-r[1-6]$", "", j.split("/")[4])) for j, a in att.items() if a["ok"]}
            if DIRS - got:
                notes.append(f"missing directions: {sorted(DIRS - got)}")
    failed = [j for j, a in att.items() if not a["ok"]]
    gen, gen_missing = minutes([a["wall_s"] for a in chosen])
    total, total_missing = minutes([a["wall_s"] for a in att.values()])
    if gen_missing or total_missing:
        notes.append("wall_s missing on some attempts: times are partial")
    mems = [(a["man"].get("run") or {}).get("host_mem_available_gib") or {} for a in chosen if a.get("man")]
    drops = [x["before"] - x["min"] for x in mems if x.get("before") is not None and x.get("min") is not None]

    rep = prov = None
    if sel:
        rp = sdir / "processed" / "report.json"
        if not rp.exists():
            notes.append("not processed")
        else:
            r = json.loads(rp.read_text())
            p = r.get("provenance") or {}
            if p.get("selection_sha256") == sha256(sel_path):
                rep, prov = r, p
            else:
                notes.append("report not bound to selection")
    if rep:
        notes.append(f"validator {'pass' if rep['pass'] else 'FAIL'} ({len(rep['failures'])} issues)")
    if failed:
        notes.append(f"failed/refused attempts: {', '.join(j.split('/', 1)[1] for j in failed)}")

    rv = review.get("sets", {}).get("/".join(x for x in (track, char, seed, variant) if x), {})
    sheet = (prov or {}).get("outputs", {}).get("sheet.png", "")
    validator = "" if rep is None else ("yes" if rep["pass"] else "no")
    human = rv.get("usable", "")
    binding = ("" if not rv else "unbound" if not rv.get("artifact_sha256")
               else "bound" if rv["artifact_sha256"] == sheet else "stale")
    if binding == "stale":
        notes.append("review was of a different artifact")
    if rv.get("notes"):
        notes.append(rv["notes"])
    if rv:
        notes.append(f"review: {review.get('reviewer', '?')}")
    ps = (prov or {}).get("process_seconds")
    fs = rep["cycle"].get("foot_slide_max") if rep else None
    return {
        "date": date_dir.name, "track": track, "character": CHAR_NO.get(char, char), "seed": seed, "variant": variant,
        "attempts": len(att), "failed_attempts": len(failed), "gen_minutes": gen, "attempt_minutes_total": total,
        "process_seconds": "" if ps is None else round(ps, 1), "cleanup_minutes": rv.get("cleanup_minutes", ""),
        "human_usable": human, "validator_pass": validator,
        "usable": combine(human if binding == "bound" else "", validator),
        "frames_fixed": rv.get("frames_fixed", ""), "back_row_accessories_kept": rv.get("back_row_accessories_kept", ""),
        "foot_slide_max_px": "" if fs is None else fs,
        "off_palette_before": sum(f.get("off_palette_before", 0) for f in rep["frames"]) if rep else "",
        "peak_mem_gb": round(max(drops), 1) if drops else "", "processed_sha256": sheet, "review_binding": binding,
        "notes": "; ".join(notes)}


def rows(date_dir: Path, review: dict) -> list[dict]:
    sets: dict[tuple, list[dict]] = {}
    for p in sorted(date_dir.rglob("manifest.json")):
        m = json.loads(p.read_text())
        k = set_key(m.get("job", ""))
        if k:
            sets.setdefault(k, []).append(m)
    return [row_for(date_dir, k, mans, review) for k, mans in sorted(sets.items())]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("date_dir", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--review", type=Path)
    a = ap.parse_args(argv)
    review = json.loads(a.review.read_text()) if a.review else {}
    rs = rows(a.date_dir, review)
    with a.out.open("w", newline="") as f:
        w = csv.DictWriter(f, COLS)
        w.writeheader()
        w.writerows(rs)
    print(f"{len(rs)} rows -> {a.out}")


if __name__ == "__main__":
    main()
