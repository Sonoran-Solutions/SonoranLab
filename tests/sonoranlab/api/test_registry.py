"""Shared validation/error boundary, including the subset used by later tools."""

import importlib
import math
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from sonoranlab.api import Context, Tool, ToolError, call, registry
from sonoranlab.api.registry import ENVELOPE_SCHEMA, ERROR_CODES


@pytest.fixture
def install(monkeypatch):
    modules = importlib.import_module("sonoranlab.api.tools")

    def add(handler=lambda params, ctx: params, schema=None, gate=None):
        tool = Tool("test", "test tool", schema or {"type": "object"}, True, "sync", handler, gate)
        monkeypatch.setattr(modules, "MODULES", (SimpleNamespace(TOOLS=(tool,)),))
        return tool

    return add


def test_registry_metadata_and_schemas():
    assert set(registry()) == {"capabilities", "list_projects", "get_profile"}
    for tool in registry().values():
        Draft202012Validator.check_schema(tool.input_schema)
        assert tool.read_only and tool.kind == "sync" and tool.version == "1"
        assert tool.annotations == {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
    Draft202012Validator.check_schema(ENVELOPE_SCHEMA)


@pytest.mark.parametrize("params", [None, [], "bad", 1, True, {"surprise": 1}])
def test_root_type_and_additional_properties(params):
    result = call("capabilities", params)
    Draft202012Validator(ENVELOPE_SCHEMA).validate(result)
    assert result["error"]["code"] == "invalid_params"


@pytest.mark.parametrize(
    ("schema", "params"),
    [
        ({"type": "integer"}, True),
        ({"type": "string", "pattern": "^[a-z]+$"}, "a/../b"),
        ({"enum": ["E", "C"]}, "A"),
        ({"type": "number", "minimum": 40}, 39),
        ({"type": "number", "maximum": 60}, 61),
        ({"type": "array", "maxItems": 2, "items": {"type": "string"}}, ["a", "b", "c"]),
        ({"type": "array", "items": {"type": "string"}}, [1]),
    ],
)
def test_schema_keywords_reject_before_handler(install, schema, params):
    def handler(params, ctx):
        pytest.fail("invalid arguments reached handler")

    install(handler, {"type": "object", "required": ["value"], "properties": {"value": schema}})
    result = call("test", {"value": params})
    assert result["error"]["code"] == "invalid_params"
    assert result["error"]["field"].startswith("value")


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, object()])
def test_non_json_parameters(install, value):
    install()
    assert call("test", {"value": value})["error"]["code"] == "invalid_params"


def test_valid_schema_boundaries_and_context(install, tmp_path):
    schema = {
        "type": "object",
        "required": ["n"],
        "properties": {"n": {"type": "integer", "minimum": 40, "maximum": 60}},
    }
    ctx = Context(tmp_path)

    def handler(params, supplied):
        assert supplied is ctx
        return params

    install(handler, schema)
    for n in (40, 60):
        assert call("test", {"n": n}, ctx=ctx)["result"] == {"n": n}
    assert call("test", {}, ctx=ctx)["error"]["field"] == "n"


@pytest.mark.parametrize("code", ERROR_CODES)
def test_explicit_contract_errors(install, code):
    def handler(params, ctx):
        raise ToolError(code, "expected", field="input", retryable=True, hint="try explicitly")

    install(handler)
    result = call("test", {})
    Draft202012Validator(ENVELOPE_SCHEMA).validate(result)
    assert result["error"] == {
        "code": code,
        "message": "expected",
        "field": "input",
        "retryable": True,
        "hint": "try explicitly",
    }


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (FileNotFoundError("missing"), "not_found"),
        (FileExistsError("exists"), "conflict"),
        (PermissionError("denied"), "path_refused"),
        (ValueError("bad"), "invalid_params"),
        (ConnectionError("offline"), "unavailable"),
        (TimeoutError("late"), "unavailable"),
        (RuntimeError("secret details"), "internal"),
    ],
)
def test_exception_mapping(install, capsys, exception, code):
    def handler(params, ctx):
        raise exception

    install(handler)
    result = call("test", {})
    Draft202012Validator(ENVELOPE_SCHEMA).validate(result)
    assert result["error"]["code"] == code
    assert capsys.readouterr().out == ""
    if code == "internal":
        assert result["error"]["message"] == "RuntimeError"
        assert "secret" not in str(result)


def test_unknown_disabled_and_duplicate_tools(install, monkeypatch):
    tool = install(gate=lambda ctx: False)
    assert registry() == {}
    assert call("test", {})["error"]["code"] == "invalid_params"
    assert call("missing", {})["error"]["code"] == "invalid_params"
    modules = importlib.import_module("sonoranlab.api.tools")
    tool = install()
    monkeypatch.setattr(modules, "MODULES", (SimpleNamespace(TOOLS=(tool, tool)),))
    with pytest.raises(ValueError, match="Duplicate tool"):
        registry()


def test_non_object_result_is_internal(install):
    install(lambda params, ctx: [])
    assert call("test", {})["error"]["code"] == "internal"


@pytest.mark.parametrize("result", [{"bad": math.nan}, {"bad": object()}])
def test_unserializable_results_are_internal(install, result):
    install(lambda params, ctx: result)
    envelope = call("test", {})
    Draft202012Validator(ENVELOPE_SCHEMA).validate(envelope)
    assert envelope["error"]["code"] == "internal"


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_profiles_must_be_regular_repository_files(tmp_path, kind):
    from sonoranlab.api.registry import REPO_ROOT

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    path = profiles / "bad.yaml"
    if kind == "symlink":
        path.symlink_to(REPO_ROOT / "profiles/twos-are-up.yaml")
    else:
        path.mkdir()
    result = call("list_projects", {}, ctx=Context(tmp_path))
    assert result["error"]["code"] == "path_refused"
