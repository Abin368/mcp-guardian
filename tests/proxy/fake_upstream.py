"""In-process fake upstream MCP server with an execution counter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server


@dataclass
class ExecutionCounter:
    """Counts how many times upstream tools actually ran."""

    calls: int = 0
    by_tool: dict[str, int] = field(default_factory=dict)
    last_arguments: dict[str, Any] | None = None

    def record(self, tool_name: str, arguments: dict[str, Any] | None) -> None:
        self.calls += 1
        self.by_tool[tool_name] = self.by_tool.get(tool_name, 0) + 1
        self.last_arguments = dict(arguments or {})


def make_fake_upstream(
    counter: ExecutionCounter,
    *,
    name: str = "fake-upstream",
) -> Server[Any]:
    """Build a minimal upstream Server: ``echo`` and ``fail`` tools."""

    async def on_list_tools(
        ctx: ServerRequestContext[Any],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="echo",
                    description="Echo a message (counts as one upstream execution)",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "message": {"type": "string"},
                        },
                        "required": ["message"],
                    },
                ),
                types.Tool(
                    name="fail",
                    description="Return a tool-level error after executing",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "reason": {"type": "string"},
                        },
                    },
                ),
            ]
        )

    async def on_call_tool(
        ctx: ServerRequestContext[Any],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        arguments = dict(params.arguments or {})
        counter.record(params.name, arguments)

        if params.name == "fail":
            reason = str(arguments.get("reason", "upstream_failed"))
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=reason)],
                isError=True,
            )

        if params.name == "echo":
            message = str(arguments.get("message", ""))
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=f"echo:{message}:count={counter.calls}",
                    )
                ],
            )

        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=f"Unknown tool: {params.name}",
                )
            ],
            isError=True,
        )

    return Server(
        name,
        version="0.0.1",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
