"""Registry operations shared by CLI and MCP; reference approval is deliberately absent."""

from sonoranlab import registry as operations
from sonoranlab.api.registry import Tool

_ID = {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]*$", "maxLength": 128}
_PATH = {"type": "string", "minLength": 1, "maxLength": 4096}


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TOOLS = (
    Tool(
        "list_characters",
        "List profile and registered characters with verified reference approval.",
        _schema({"project": _ID}, ["project"]),
        True,
        "sync",
        lambda p, c: operations.list_characters(**p, ctx=c),
    ),
    Tool(
        "get_character",
        "Read reference hashes, approval evidence, imported jobs and current human review.",
        _schema({"project": _ID, "character": _ID}, ["project", "character"]),
        True,
        "sync",
        lambda p, c: operations.get_character(**p, ctx=c),
    ),
    Tool(
        "register_character",
        "Copy the profile still set into immutable STATE references, initially unapproved.",
        _schema(
            {"project": _ID, "character": _ID, "reference_dir": _PATH, "notes": {"type": "string", "maxLength": 65536}},
            ["project", "character", "reference_dir"],
        ),
        False,
        "sync",
        lambda p, c: operations.register_character(**p, ctx=c),
    ),
    Tool(
        "import_job",
        "Index an existing job without copying or modifying it; record artifact provenance.",
        _schema({"project": _ID, "character": _ID, "job_dir": _PATH}, ["project", "character", "job_dir"]),
        False,
        "sync",
        lambda p, c: operations.import_job(**p, ctx=c),
    ),
    Tool(
        "get_artifact",
        "Read bounded artifact metadata (never pixels or text bodies).",
        _schema({"ref": _PATH}, ["ref"]),
        True,
        "sync",
        lambda p, c: operations.get_artifact(**p, ctx=c),
    ),
    Tool(
        "check_review",
        "Recompute human review and production usability from current job files.",
        _schema(
            {"job": {"type": "string", "pattern": "^(job-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}|import-[a-z0-9]+)$"}}, ["job"]
        ),
        True,
        "sync",
        lambda p, c: operations.check_review(**p, ctx=c),
    ),
)
