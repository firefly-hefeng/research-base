"""Optional local MCP adapter. Importing research_base does not require MCP."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .models import ResearchError
from .tools import ToolRegistry


def serve(library_path: str | Path) -> None:
    """Run a trusted same-host stdio server using the official SDK."""
    try:
        import mcp.types as types
        from mcp.server.lowlevel import Server
        from mcp.server.stdio import stdio_server
    except ImportError:
        raise ResearchError("MCP_NOT_INSTALLED", "Install the MCP extra: pip install -e .[mcp]") from None
    from .core import ResearchBase

    core = ResearchBase(library_path)
    registry = ToolRegistry(core, allow_writes=True, local_admin=True)
    server = Server("research-base")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(name=item["function"]["name"], description=item["function"]["description"],
                           inputSchema=item["function"]["parameters"])
                for item in registry.specs()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> types.CallToolResult:
        result = await asyncio.to_thread(registry.call, name, arguments)
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                                    isError="error" in result)

    async def main() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    try:
        asyncio.run(main())
    finally:
        core.close()
