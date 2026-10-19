"""Phase 1 scorer: one benchmark/scoring.csv row per (track, character, seed) from manifests + validator reports.

Machine columns come from the files; human columns (cleanup_minutes, usable, frames_fixed, back_row_accessories_kept)
come from a review JSON (owner or agent review, its `reviewer` field is copied into notes) and stay blank without one.
gen_minutes = sum of successful job wall times for the set (C/E: all 5 directions; A/B: the one grid); failed and
refused attempts are listed in notes, not counted. peak_mem_gb = largest host MemAvailable drop across the set.

    uv run --no-project python -I benchmark/tools/score.py RESULTS_DATE_DIR OUT.csv [--review review.json]
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

COLS = ["date", "track", "character", "seed", "gen_minutes", "process_seconds", "cleanup_minutes", "usable",
        "frames_fixed", "back_row_accessories_kept", "foot_slide_max_px", "off_palette_before", "peak_mem_gb", "notes"]
CHAR_NO = {"samurai": 1, "oathguard": 2, "relay_sentinel": 3}


def ok(m: dict) -> bool:
    return m.get("exit_code") == 0


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


def report_for(date_dir: Path, track: str, char: str, seed: str, variant: str) -> Path | None:
    base = date_dir / track / char / seed
    for c in [base / "clip" / "proc-t0", base / "clip-r1024" / "proc-t0", base / variant / "processed-t0",
              base / "processed-t0", base / "smoke-try2" / "processed-t0"]:
        if (c / "report.json").exists():
            return c / "report.json"
    return None


def rows(date_dir: Path, review: dict) -> list[dict]:
    sets: dict[tuple, list[dict]] = {}
    for p in sorted(date_dir.rglob("manifest.json")):
        m = json.loads(p.read_text())
        k = set_key(m.get("job", ""))
        if k:
            sets.setdefault(k, []).append(m)
    out = []
    for (track, char, seed, variant), mans in sorted(sets.items()):
        good = [m for m in mans if ok(m)]
        bad = [m["job"].split("/", 1)[1] for m in mans if not ok(m)]
        if not good:
            continue
        gen_s = sum(m["run"].get("wall_s") or 0 for m in good)
        drops = [m["run"]["host_mem_available_gib"]["before"] - m["run"]["host_mem_available_gib"]["min"]
                 for m in good if (m["run"].get("host_mem_available_gib") or {}).get("min") is not None]
        rep_path = report_for(date_dir, track, char, seed, variant)
        rep = json.loads(rep_path.read_text()) if rep_path else None
        key = "/".join(x for x in (track, char, seed, variant) if x)
        rv = review.get("sets", {}).get(key, {})
        notes = [variant] if variant else []
        notes.append(f"{len(good)} jobs")
        if track in "CE":
            missing = DIRS - {re.sub(r"-r1024$", "", re.sub(r"-r[1-6]$", "", m["job"].split("/")[4])) for m in good}
            if missing:
                notes.append(f"missing directions: {sorted(missing)}")
        notes.append(f"validator {'pass' if rep['pass'] else 'FAIL'} ({len(rep['failures'])} issues)" if rep
                     else "not processed")
        if bad:
            notes.append(f"failed/refused attempts: {', '.join(bad)}")
        if rv.get("notes"):
            notes.append(rv["notes"])
        if rv:
            notes.append(f"review: {review.get('reviewer', '?')}")
        out.append({
            "date": date_dir.name, "track": track, "character": CHAR_NO.get(char, char), "seed": seed,
            "gen_minutes": round(gen_s / 60, 1), "process_seconds": rv.get("process_seconds", ""),
            "cleanup_minutes": rv.get("cleanup_minutes", ""), "usable": rv.get("usable", ""),
            "frames_fixed": rv.get("frames_fixed", ""), "back_row_accessories_kept": rv.get("back_row_accessories_kept", ""),
            "foot_slide_max_px": rep["cycle"].get("foot_slide_max", "") if rep else "",
            "off_palette_before": sum(f.get("off_palette_before", 0) for f in rep["frames"]) if rep else "",
            "peak_mem_gb": round(max(drops), 1) if drops else "", "notes": "; ".join(notes)})
    return out


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
