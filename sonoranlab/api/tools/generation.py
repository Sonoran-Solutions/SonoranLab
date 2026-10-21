"""Narrow generation submissions; no caller graphs, prompts or output paths."""

from sonoranlab.api.registry import Tool
from sonoranlab.jobs import manager

ID = {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]*$"}
SEED = {"type": "integer", "minimum": 0, "maximum": 2**31 - 1}
COMMON = {
    "project": ID,
    "character": ID,
    "ref_set_id": {"type": "string", "pattern": "^refs-[0-9a-f]{12}$"},
    "allow_pause": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True, "default": []},
    "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 200},
}


def schema(properties, required):
    return {
        "type": "object",
        "properties": COMMON | properties,
        "required": ["project", "character", "ref_set_id", *required],
        "additionalProperties": False,
    }


ANIMATE_SCHEMA = schema(
    {
        "anim": ID,
        "seeds": {"type": "array", "items": SEED, "minItems": 1, "maxItems": 4, "uniqueItems": True},
        "windows": {"enum": ["default", "steady"], "default": "default"},
        "godot_check": {"type": "boolean", "default": True},
        "need_gib": {"type": "number", "minimum": 40, "default": 40},
    },
    ["anim", "seeds"],
)
POSE_SCHEMA = schema(
    {
        "direction": ID,
        "template": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_-]*/pose_[0-9]{3}$"},
        "seed": SEED,
    },
    ["direction", "template", "seed"],
)


def submit(kind, params):
    from sonoranlab.jobs.kinds.generation import replay

    return replay(kind, params) or manager.submit(kind, params, params.get("idempotency_key"))


TOOLS = (
    Tool(
        "animate",
        "Generate a Track E profile animation from approved references; owner review remains required.",
        ANIMATE_SCHEMA,
        False,
        "job",
        lambda p, c: submit("animate", p),
    ),
    Tool(
        "pose_frame",
        "Track C single pose / posed first frame; no automatic C->E fallback.",
        POSE_SCHEMA,
        False,
        "job",
        lambda p, c: submit("pose_frame", p),
    ),
)
