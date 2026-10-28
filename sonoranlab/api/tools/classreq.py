"""Class-request admission (ADR-0011): read-only, the same check as `sonoranlab classreq check`.

The agent passes the parsed request object; stills come only from a registered reference set (`stills_ref`), never
from a path inside the untrusted request text. No contact image is written. `refprep` is CLI-only (owner step).
"""

from __future__ import annotations

from sonoranlab import classreq
from sonoranlab import registry as records
from sonoranlab.api import paths
from sonoranlab.api.registry import Context, Tool, ToolError

_ID = {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]*$", "maxLength": 128}
_REF = {"type": "string", "pattern": "^refs-[0-9a-f]{12}$"}


def check_class_request(project: str, request: dict, stills_ref: str | None = None, *, ctx: Context) -> dict:
    from sonoranlab.profile import load_palette

    profile = records._profile(project, ctx)
    character = request.get("character")
    valid_id = isinstance(character, str) and bool(classreq.NAME_RE.match(character))
    stills_dir = None
    if stills_ref is not None:
        if not valid_id:
            raise ToolError("invalid_params", "stills_ref needs a valid request character", field="stills_ref")
        stills_dir = records._base(project, character, ctx) / "refs" / stills_ref
        if not stills_dir.is_dir():
            raise ToolError("not_found", "Unknown reference set for this project/character", field="stills_ref")
        stills_dir = records._directory(stills_dir, ctx)
        for d in profile.directions.order:
            if (stills_dir / f"still_{d}.png").exists():
                paths.resolve_input(stills_dir / f"still_{d}.png")
    reg = None
    if valid_id:
        records._base(project, character, ctx)  # confinement of the record path
        reg = classreq.read_registry(paths.state_root(), project, character)
    result, _ = classreq.check(
        request, profile, load_palette(profile), stills_dir=stills_dir, stills_source=stills_ref, registry=reg
    )
    return result


TOOLS = (
    Tool(
        "check_class_request",
        "Check a class request and its registered eight stills against the profile; status and itemised findings.",
        {
            "type": "object",
            "properties": {"project": _ID, "request": {"type": "object", "maxProperties": 32}, "stills_ref": _REF},
            "required": ["project", "request"],
            "additionalProperties": False,
        },
        True,
        "sync",
        lambda p, c: check_class_request(**p, ctx=c),
    ),
)
