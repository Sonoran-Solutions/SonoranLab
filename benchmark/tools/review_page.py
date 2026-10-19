"""Build the local human-review page for the Phase 1 sets (disposable tooling; the page stays on this machine).

For every processed set under RESULTS_DATE_DIR it shows the game-layout sheet's generated rows (N, NE, E, SE, S)
playing at the clip fps, at true 1x and 3x (nearest), next to column 0 (the shipped still), plus a form for the
usable rubric (benchmark/characters/2026-10-18.yaml). Answers autosave in the browser and export as review.json,
which benchmark/tools/score.py --review reads. The page references the sheets by relative path; nothing is uploaded.

    uv run --no-project python -I benchmark/tools/review_page.py ~/sonoranlab-work/benchmark/results/2026-10-18 \
      ~/sonoranlab-work/benchmark/review-2026-10-18
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROWS = {"north": 0, "north-east": 7, "east": 6, "south-east": 5, "south": 4}   # game sheet row order N,NW,W,SW,S,SE,E,NE
CLIP = {"samurai": (184, 7, 10), "oathguard": (176, 6, 10), "relay_sentinel": (136, 4, 5)}   # cell, frames, fps
GRID = {"A": (48, 3, 6), "B": (32, 3, 6)}   # svntax walk columns, cell after /4, fps for a 3-frame walk


def find_sets(date_dir: Path) -> list[dict]:
    sets = []
    for sheet in sorted(date_dir.rglob("sheet.png")):
        rel = sheet.relative_to(date_dir).parts
        track, char, seed = rel[0], rel[1], rel[2].split("-r")[0]
        if track not in "ABCE" or char not in CLIP:
            continue
        if track == "C" and "clip-r1024" not in rel:
            continue
        if track == "E" and "clip" not in rel:
            continue
        if track in "AB" and ("dyl77" in "/".join(rel) or "processed-t0" not in rel):
            continue
        variant = rel[3] if track == "B" else ""
        if track == "B" and not variant.startswith("ck"):
            continue
        cell, frames, fps = GRID[track] if track in "AB" else CLIP[char]
        key = "/".join(x for x in (track, char, seed, variant) if x)
        sets.append({"key": key, "track": track, "char": char, "seed": seed, "sheet": str(sheet), "cell": cell,
                     "frames": frames, "fps": fps, "rows": ROWS})
    return sets


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Phase 1 review</title>
<style>
:root{--bg:#1d1e22;--fg:#e6e6e6;--mut:#9a9aa3;--card:#26272c;--line:#3a3b42;--acc:#7cc4ff}
body{background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif;margin:16px}
h1{font-size:18px} .set{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin:12px 0}
.row{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;margin:6px 0} .lab{width:80px;color:var(--mut)}
canvas{image-rendering:pixelated;background:#45464d;border:1px solid var(--line)}
form{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;margin-top:8px}
label{display:flex;flex-direction:column;gap:2px;color:var(--mut)} input,select,textarea{background:#15161a;color:var(--fg);border:1px solid var(--line);border-radius:4px;padding:4px}
button{background:var(--acc);color:#000;border:0;border-radius:4px;padding:6px 10px;font-weight:600;cursor:pointer}
.bar{position:sticky;top:0;background:var(--bg);padding:8px 0;display:flex;gap:8px;align-items:center;z-index:2}
.rub{color:var(--mut);font-size:12px}
</style></head><body>
<div class="bar"><h1 style="margin:0">SonoranLab Phase 1 review, 2026-10-18</h1>
<input id="who" placeholder="reviewer name"><button onclick="exportJson()">Export review.json</button><span id="saved" class="rub"></span></div>
<p class="rub">Usable only if every line holds (benchmark/characters/2026-10-18.yaml): identity in every direction;
every accessory in every direction, incl. north; correct facing (west rows are the game's east mirrors);
reads as the clip at its fps (1x) without popping, loop closes; no baked VFX/background/sticker border.
Cleanup minutes = your estimate (or stopwatch) to make it pass in Aseprite/Pixelorama; frames_fixed = frames needing edits.
Column 0 is the shipped still.</p>
<div id="sets"></div>
<script>
const SETS = __SETS__;
const KEY = 'sonoranlab-review-2026-10-18';
let store = {}; try { store = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) {}
function save(){ try { localStorage.setItem(KEY, JSON.stringify(store)); document.getElementById('saved').textContent='saved'; } catch(e){} }
const root = document.getElementById('sets');
for (const s of SETS) {
  const div = document.createElement('div'); div.className = 'set';
  div.innerHTML = `<b>${s.key}</b> <span class="rub">${s.frames} frames @ ${s.fps} fps, cell ${s.cell}</span>`;
  const img = new Image(); img.src = s.rel;
  const cans = [];
  for (const [dir, row] of Object.entries(s.rows)) {
    const r = document.createElement('div'); r.className = 'row';
    r.innerHTML = `<span class="lab">${dir}</span>`;
    for (const z of [1, 3]) { const c = document.createElement('canvas'); c.width = s.cell*z; c.height = s.cell*z; r.appendChild(c); cans.push([c, row, z, false]); }
    const st = document.createElement('canvas'); st.width = s.cell*3; st.height = s.cell*3; r.appendChild(st); cans.push([st, row, 3, true]);
    div.appendChild(r);
  }
  let f = 0;
  img.onload = () => setInterval(() => {
    for (const [c, row, z, still] of cans) {
      const g = c.getContext('2d'); g.imageSmoothingEnabled = false; g.clearRect(0,0,c.width,c.height);
      const col = still ? 0 : 1 + (f % s.frames);
      g.drawImage(img, col*s.cell, row*s.cell, s.cell, s.cell, 0, 0, s.cell*z, s.cell*z);
    }
    f++;
  }, 1000 / s.fps);
  const v = store[s.key] || {};
  const form = document.createElement('form');
  form.innerHTML = `
   <label>usable<select name="usable"><option></option><option>yes</option><option>no</option></select></label>
   <label>cleanup_minutes<input name="cleanup_minutes" type="number" min="0" step="1"></label>
   <label>frames_fixed<input name="frames_fixed" type="number" min="0" step="1"></label>
   <label>back_row_accessories_kept<input name="back_row_accessories_kept" placeholder="katana:yes,shield:no"></label>
   <label style="grid-column:1/-1">notes<textarea name="notes" rows="2"></textarea></label>`;
  for (const el of form.elements) { if (v[el.name] !== undefined) el.value = v[el.name];
    el.addEventListener('input', () => { store[s.key] = Object.fromEntries(new FormData(form)); save(); }); }
  div.appendChild(form); root.appendChild(div);
}
function exportJson(){
  const sets = {}; for (const [k, v] of Object.entries(store)) { const o = {...v};
    for (const n of ['cleanup_minutes','frames_fixed']) if (o[n] !== '' && o[n] !== undefined) o[n] = Number(o[n]);
    sets[k] = o; }
  const blob = new Blob([JSON.stringify({reviewer: document.getElementById('who').value || 'owner', sets}, null, 1)], {type: 'application/json'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'review.json'; a.click();
}
</script></body></html>
"""


def main(date_dir: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    sets = find_sets(date_dir)
    for s in sets:
        s["rel"] = os.path.relpath(s.pop("sheet"), out)
    (out / "index.html").write_text(PAGE.replace("__SETS__", json.dumps(sets)))
    print(f"{len(sets)} sets -> {out / 'index.html'}")


if __name__ == "__main__":
    main(Path(sys.argv[1]).expanduser(), Path(sys.argv[2]).expanduser())
