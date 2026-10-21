"""One registry, validation boundary and result envelope for both front ends."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

if TYPE_CHECKING:
    from sonoranlab.profile import Profile

API_SCHEMA = "sonoranlab.api/1"
SERVER_VERSION = version("sonoranlab")
REPO_ROOT = Path(__file__).resolve().parents[2]
ERROR_CODES = (
    "invalid_params",
    "not_found",
    "conflict",
    "not_ready",
    "idempotency_conflict",
    "path_refused",
    "unavailable",
    "refused",
    "budget_refused",
    "guard_busy",
    "internal",
)
ENVELOPE_SCHEMA = {
    "type": "object",
    "required": ["schema", "tool", "ok"],
    "properties": {
        "schema": {"const": API_SCHEMA},
        "tool": {"type": "string"},
        "ok": {"type": "boolean"},
        "result": {"type": "object"},
        "error": {
            "type": "object",
            "required": ["code", "message", "field", "retryable", "hint"],
            "properties": {
                "code": {"enum": list(ERROR_CODES)},
                "message": {"type": "string"},
                "field": {"type": ["string", "null"]},
                "retryable": {"type": "boolean"},
                "hint": {"type": ["string", "null"]},
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
    "oneOf": [
        {"properties": {"ok": {"const": True}}, "required": ["result"], "not": {"required": ["error"]}},
        {"properties": {"ok": {"const": False}}, "required": ["error"], "not": {"required": ["result"]}},
    ],
}


@dataclass(frozen=True)
class Context:
    repo: Path = REPO_ROOT

    def profiles(self) -> Iterator[Profile]:
        """Repository-owned profiles; never open the external palette or game checkout."""
        from sonoranlab.profile import load_profile

        for path in sorted((self.repo / "profiles").glob("*.yaml")):
            if (
                path.is_symlink()
                or not path.is_file()
                or not path.resolve(strict=True).is_relative_to(self.repo.resolve(strict=True))
            ):
                raise ToolError("path_refused", "Profile must be a regular repository file")
            yield load_profile(path, check_paths=False)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    read_only: bool
    kind: Literal["sync", "job"]
    handler: Callable[[dict, Context], dict]
    capability_gate: Callable[[Context], bool] | None = None
    version: str = "1"

    @property
    def annotations(self) -> dict:
        return {"readOnlyHint": self.read_only, "destructiveHint": False, "idempotentHint": self.read_only}

    def summary(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "input_schema": self.input_schema,
            "read_only": self.read_only,
            "kind": self.kind,
        }


class ToolError(Exception):
    """An expected contract error; handlers choose the code without changing either transport."""

    def __init__(
        self, code: str, message: str, *, field: str | None = None, retryable: bool = False, hint: str | None = None
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"Unknown API error code: {code}")
        super().__init__(message)
        self.error = {"code": code, "message": message, "field": field, "retryable": retryable, "hint": hint}


def failure(name: str, error: ToolError) -> dict:
    return {"schema": API_SCHEMA, "tool": name, "ok": False, "error": error.error}


def registry(ctx: Context | None = None) -> dict[str, Tool]:
    """Add an area module to tools.MODULES; both transports discover it here."""
    from .tools import MODULES

    ctx = ctx or Context()
    tools = {}
    for module in MODULES:
        for tool in module.TOOLS:
            if tool.name in tools:
                raise ValueError(f"Duplicate tool: {tool.name}")
            if tool.capability_gate is None or tool.capability_gate(ctx):
                tools[tool.name] = tool
    return tools


def call(name: str, params: dict, *, ctx: Context | None = None) -> dict:
    """Validate once and contain all ordinary handler errors, including serialization failures."""
    try:
        ctx = ctx or Context()
        tool = registry(ctx).get(name)
        if tool is None:
            raise ToolError("invalid_params", f"Unknown or disabled tool: {name}", field="tool")
        # Reject non-JSON numbers/objects even when a tool schema permits arbitrary metadata.
        try:
            json.dumps(params, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ToolError("invalid_params", "Parameters must be finite JSON values") from exc
        Draft202012Validator(tool.input_schema).validate(params)
        result = tool.handler(params, ctx)
        if not isinstance(result, dict):
            raise TypeError("Tool result must be an object")
        envelope = {"schema": API_SCHEMA, "tool": name, "ok": True, "result": result}
        try:
            return json.loads(json.dumps(envelope, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Handler returned non-JSON data") from exc
    except ValidationError as exc:
        field = ".".join(map(str, exc.absolute_path)) or None
        if exc.validator == "required":
            field = next(key for key in exc.validator_value if key not in exc.instance)
        return failure(name, ToolError("invalid_params", exc.message, field=field))
    except ToolError as exc:
        return failure(name, exc)
    except FileNotFoundError as exc:
        return failure(name, ToolError("not_found", str(exc)))
    except FileExistsError as exc:
        return failure(name, ToolError("conflict", str(exc)))
    except PermissionError as exc:
        return failure(name, ToolError("path_refused", str(exc)))
    except ValueError as exc:
        return failure(name, ToolError("invalid_params", str(exc)))
    except (ConnectionError, TimeoutError) as exc:
        return failure(name, ToolError("unavailable", type(exc).__name__, retryable=True))
    except Exception as exc:
        logging.getLogger(__name__).exception("Tool %s failed", name)
        return failure(name, ToolError("internal", type(exc).__name__))
