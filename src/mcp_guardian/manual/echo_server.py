"""Harmless upstream MCP server for a manual Guardian test.

One tool, ``echo``. It returns the text you sent. It does not read or write
the filesystem unless you set ``MCP_GUARDIAN_ECHO_LEDGER`` to a file path.
Each real execution then appends a single line, ``executed``, with no
arguments, so you can see whether a blocked call ran.

Run it only as the proxy's upstream, not as a server Claude launches directly:

    python -m mcp_guardian.manual.echo_server

Logs go to stderr and never include the tool arguments.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

logger = logging.getLogger("mcp_guardian.manual.echo")

_LEDGER = "MCP_GUARDIAN_ECHO_LEDGER"


def _record_execution() -> None:
    """Count one real run. The line does not contain the tool arguments."""
    logger.info("echo_tool_executed")
    raw = os.environ.get(_LEDGER)
    if not raw:
        return
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("executed\n")
        handle.flush()


def build_server() -> Server[Any]:
    async def on_list_tools(
        ctx: ServerRequestContext[Any],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="echo",
                    description=(
                        "Return the text you send. Does not touch the filesystem "
                        "except an optional execution ledger."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "message": {"type": "string"},
                        },
                        "required": ["message"],
                    },
                )
            ]
        )

    async def on_call_tool(
        ctx: ServerRequestContext[Any],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        if params.name != "echo":
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="Unknown tool")],
                isError=True,
            )
        arguments = dict(params.arguments or {})
        message = arguments.get("message", "")
        if not isinstance(message, str):
            message = ""
        try:
            _record_execution()
        except OSError:
            logger.error("echo_ledger_failed error_type=OSError")
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text="Echo ran but the ledger file could not be updated.",
                    )
                ],
                isError=True,
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"echo:{message}")],
        )

    return Server(
        "harmless-echo",
        version="0.1.0",
        instructions="Echoes one string. No filesystem access beyond an optional ledger.",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def _amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    import asyncio

    asyncio.run(_amain())


if __name__ == "__main__":
    main()
