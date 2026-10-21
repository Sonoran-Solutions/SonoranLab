"""Read-only summaries from the existing profile loader, with no palette pixel reads."""

import json
from dataclasses import asdict

from sonoranlab.api.registry import Context, Tool, ToolError


def list_projects(params: dict, ctx: Context) -> dict:
    return {
        "projects": [
            {"id": p.id, "name": p.name, "profile_path": p.path.relative_to(ctx.repo).as_posix()}
            for p in ctx.profiles()
        ]
    }


def get_profile(params: dict, ctx: Context) -> dict:
    profile = next((p for p in ctx.profiles() if p.id == params["project"]), None)
    if profile is None:
        raise ToolError("not_found", "Unknown project", field="project")
    summary = {
        "id": profile.id,
        "name": profile.name,
        "profile_path": profile.path.relative_to(ctx.repo).as_posix(),
        "canvas": asdict(profile.canvas),
        "cells_per_character": {name: profile.character(name).cell for name in profile.characters},
        "directions": {
            key: value
            for key, value in asdict(profile.directions).items()
            if key in ("order", "generate", "mirror_animations")
        },
        "animations": {name: asdict(anim) for name, anim in profile.animations.items()},
        "validator": asdict(profile.validator),
        "autonomy": asdict(profile.autonomy),
        "palette": {"path": str(profile.palette.source), "sha256": profile.palette.sha256},
        "export": {**asdict(profile.export), "read_only": True, "protected": True},
    }
    return json.loads(json.dumps(summary, default=str))


TOOLS = (
    Tool(
        "list_projects",
        "List repository project profiles (read-only).",
        {"type": "object", "properties": {}, "additionalProperties": False},
        True,
        "sync",
        list_projects,
    ),
    Tool(
        "get_profile",
        "Read a resolved profile summary; palette path and hash only.",
        {
            "type": "object",
            "properties": {"project": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]*$"}},
            "required": ["project"],
            "additionalProperties": False,
        },
        True,
        "sync",
        get_profile,
    ),
)
