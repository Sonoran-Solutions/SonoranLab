"""MCP 2.3 low-level callbacks preserve the registry's schemas and envelopes."""

from __future__ import annotations

import json
import logging

import anyio
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from sonoranlab.api import call, registry
from sonoranlab.api.registry import ENVELOPE_SCHEMA, SERVER_VERSION


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
    # Even read tools touch disk. Keeping every handler in a worker also covers future slow/mutating tools.
    envelope = await anyio.to_thread.run_sync(
        call, params.name, params.arguments if params.arguments is not None else {}
    )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(envelope))],
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
