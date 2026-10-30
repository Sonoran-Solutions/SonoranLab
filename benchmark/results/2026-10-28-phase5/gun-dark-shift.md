# Gun smoke `nearest_filtering`: investigation and gun-only exception (2026-10-29/30 UTC)

Finding: the gun smoke render fails `nearest_filtering` by 1. Two source colours, (126,91,7)→(126,91,0) and
(46,41,7)→(45,40,0), shift 7 against a dark-channel tolerance of 6. No colour splits into several, so nothing is
blended.

Cause: a deterministic sRGB conversion in Godot's Compatibility renderer, which the smoke harness forces. It reads
textures with a polynomial sRGB→linear approximation and writes them with a pow approximation that lacks the
linear segment near black. That round trip maps channel codes 0–7 to 0, so only code 7 shifts by more than 6. Thief,
engineer and pyromancer contain no 7 in any channel and pass by chance.

Evidence: seven controlled renders under the host lock (checkout 7c3095d):
- gun beside siblings and alone, each plain and with the owner's shader;
- a synthetic dark ramp, plain and with the shader;
- a repeat of the gun-alone plain render.

In every run and at both scales the model predicts every rendered colour (0 mismatches), no colour splits, and 1x
and 4x agree. The repeat is byte-identical. The game itself renders with Forward Plus, which is not rendered here.

Decision: the owner chose (2026-10-30, verbatim) "Document that I approve a gun only exception". The exception covers
gun only and those two colours in gun's approved sheets (`914dabb9…`, `862ef0ce…`). ADR-0011 O14 records it. The
global tolerance stays at 6, and the smoke check still reports FAIL for these renders (a recorded exception, not a
pass). A harness fix (compare against the known conversion curve, or render with Forward+) remains a possible
follow-up. Private evidence: `~/sonoranlab-work/phase5/gun-dark-shift/`.
