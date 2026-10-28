"""MCP 2.3 low-level callbacks preserve the registry's schemas and envelopes."""

from __future__ import annotations

import base64
import json
import logging

import anyio
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from sonoranlab.api import call, registry
from sonoranlab.api.registry import ENVELOPE_SCHEMA, SERVER_VERSION
from sonoranlab.api.tools import view


async def list_tools(ctx: ServerRequestContext, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
    tools = await anyio.to_thread.run_sync(registry)
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
                output_schema=ENVELOPE_SCHEMA,
                annotations=types.ToolAnnotations.model_validate(tool.annotations),
            )
            for tool in tools.values()
        ]
    )


async def call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    def run(name: str, arguments: dict) -> tuple[dict, list[bytes]]:
        with view.capture() as images:  # view_artifact's PNG travels as ImageContent, never inside the envelope
            return call(name, arguments), images

    # Even read tools touch disk. Keeping every handler in a worker also covers future slow/mutating tools.
    envelope, images = await anyio.to_thread.run_sync(
        run, params.name, params.arguments if params.arguments is not None else {}
    )
    pictures = [
        types.ImageContent(type="image", data=base64.b64encode(image).decode(), mime_type="image/png")
        for image in (images if envelope["ok"] else [])
    ]
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(envelope)), *pictures],
        structured_content=envelope,
        is_error=not envelope["ok"],
    )


def create_server() -> Server:
    return Server("sonoranlab", version=SERVER_VERSION, on_list_tools=list_tools, on_call_tool=call_tool)


async def serve() -> None:
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        logging.getLogger(__name__).info("SonoranLab stdio server ready")
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> int:
    logging.basicConfig(level=logging.INFO, force=True)
    anyio.run(serve)
    return 0
