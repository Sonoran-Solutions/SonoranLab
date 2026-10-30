"""`smoke_render` (DYL-131): typed async job; inputs are registry/job/artifact refs, never paths or scenes."""

from __future__ import annotations

from sonoranlab import registry as records
from sonoranlab.api.registry import Context, Tool
from sonoranlab.jobs import manager

STRING = {"type": "string", "minLength": 1}
SCHEMA = {
    "type": "object",
    "properties": {
        "project": STRING,
        "character": STRING,
        "candidate": {**STRING, "description": "Job, import or artifact ref of the candidate's export (not a path)."},
        "siblings": {
            "type": "array",
            "items": STRING,
            "maxItems": 3,
            "uniqueItems": True,
            "description": "Registry-imported refs of approved sibling exports or staged game sheets (docs/09).",
        },
        "idempotency_key": STRING,
    },
    "required": ["project", "character", "candidate", "siblings"],
    "additionalProperties": False,
}


def submit(params: dict, ctx: Context) -> dict:
    records._character(params["project"], params["character"], ctx)
    return manager.submit("smoke_render", params, params.get("idempotency_key"))


TOOLS = (
    Tool(
        "smoke_render",
        "Render a candidate beside approved siblings in an isolated Godot project; objective pixel checks only.",
        SCHEMA,
        False,
        "job",
        submit,
    ),
)
