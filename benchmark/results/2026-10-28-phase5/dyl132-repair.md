# DYL-132 — optional live repair lifecycle (2026-10-28 UTC): STOPPED at admission, NOT_RUN

Owner authorization (verbatim): "Yes, one bounded edit" — one few-pixel edit by the contained editor on a disposable
copy of the approved Phase 4 pyromancer job; the child needs a fresh owner review.

| Step | Result |
|---|---|
| 1 disposable parent copies (Phase 3 export-v2, Phase 4 final job), originals hashed before/after | PASS: originals and copies byte-identical |
| 2 fresh MCP client: capabilities (`editor: available`, 28 tools), `import_job` of each copy | **STOP**: both refused — `path_refused` (the Phase 3 manifest's profile lives in a removed worktree outside READ roots) and `Animate final job escapes its directory` (the Phase 4 `animate.json` points at the original job and a removed orchestrator worktree). No manifest was edited. |
| 3–7 authorize, broker + relay, contained gpt-6.1-sol editor, diff, finalize, child verdicts | NOT_RUN (no parent) |
| 8 unavailable runtime → repair tools absent; direct `prepare_repair` → `Unknown or disabled tool` | PASS |

Same root cause as the Phase 4 approval finding: older manifests bind absolute paths of worktrees that no longer exist.
The Phase 5 profile-row binding (PR #65) verifies a changed profile against git, but it does not make a missing
profile path resolvable. Containment for the current Codex version was not re-verified (last proof: DYL-110).

Owner options (NEEDS_OWNER_INPUT): authorize the bounded edit on a disposable copy of a Phase 5 class job instead
(e.g. the approved thief compose, whose paths resolve); regenerate a pyromancer parent; or defer DYL-132 to Phase 6
with the route kept disabled (ADR-0011 default). Full private evidence: `~/sonoranlab-work/phase5/dyl132/`.
