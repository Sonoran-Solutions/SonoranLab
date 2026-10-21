"""Startup-safe capability discovery; presence checks only, never service probes."""

import os
import shutil
from pathlib import Path

from sonoranlab.api.registry import API_SCHEMA, SERVER_VERSION, Context, Tool, registry


def capabilities(params: dict, ctx: Context) -> dict:
    runtime = Path(os.environ.get("SONORANLAB_EDITOR_RUNTIME", "~/sonoranlab-work/phase3.5/runtime")).expanduser()
    return {
        "server_version": SERVER_VERSION,
        "api_schema": API_SCHEMA,
        "tools": [tool.summary() for tool in registry(ctx).values()],
        "comfyui": {
            "container": os.environ.get("SONORANLAB_COMFY_CONTAINER", "sonoranlab-comfy"),
            "status": "not_probed",
        },
        "godot": {"path": shutil.which("godot")},
        "editor": "available" if runtime.is_dir() else "unavailable",
        "offline_policy": "Local execution only; no downloads or cloud fallback. Images/contact sheets requested "
        "by an agent may be disclosed to its owner-approved model provider. Runtime presence is not a health probe.",
    }


TOOLS = (
    Tool(
        "capabilities",
        "Discover local capabilities without launching or probing services.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        True,
        "sync",
        capabilities,
    ),
)
