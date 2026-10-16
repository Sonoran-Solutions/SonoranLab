# ADR-0003 — Pose/motion templates are separate from appearance

**Status:** accepted (2026-10-16)

## Context

A sprite animation has two independent sources of consistency: *what the
character looks like* and *where its limbs are on each frame*. Grid LoRAs
entangle both in one prompt. PixelLab's skeleton tool separates them and is
the only hosted product that gets animation reliably right.

## Decision

- Animations are defined as reusable **templates** (`templates/poses/<anim>/`)
  produced from a rigged 3D bake (Track D) or hand-authored: one OpenPose
  render (and optionally depth) per direction per frame.
- Appearance comes from the approved character reference.
- A track is a way to combine the two: ControlNet conditioning (C) or seeding
  a video model with a posed first frame (E).

## Consequences

- One approved template is reused across every character; fixing timing or
  a weapon arc fixes it for all of them.
- Monsters with unusual bodies need their own templates (or a crude rig),
  which is explicit work rather than a model gamble.
- `templates/poses/` is a first-class asset with its own README and
  provenance.
