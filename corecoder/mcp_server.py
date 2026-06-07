"""MCP server exposing CoreCoder's read/grep tools over the Model Context Protocol.

This is the "tools as an MCP service" half of the fourth increment: the file
retrieval/read capabilities are decoupled from the agent process into this
standalone server. The agent (as an MCP client, see mcp_bridge.py) discovers
and calls these tools over the standard protocol instead of in-process Python.

Transport is stdio: the bridge spawns this module as a subprocess
(`python -m corecoder.mcp_server`) with cwd set to the task's repo checkout,
so relative paths resolve exactly as the in-process tools would.

HARD RULE: never print() in this process — stdout IS the MCP protocol channel
and any stray write corrupts the JSON-RPC framing. FastMCP routes its own
logging to stderr; the wrapped tools only return strings, so we're safe by
construction. If you ever need to debug here, write to sys.stderr.

The tool logic is NOT reimplemented — each MCP tool delegates to the existing
Tool classes in corecoder/tools/, so behavior is identical to the in-process
path (same truncation limits, same error strings). Only the transport differs.
"""

from mcp.server.fastmcp import FastMCP

from corecoder.tools.read import ReadFileTool
from corecoder.tools.grep import GrepTool

mcp = FastMCP("corecoder-fs")

_read = ReadFileTool()
_grep = GrepTool()


@mcp.tool()
def read_file(file_path: str, offset: int = 1, limit: int = 2000) -> str:
    """Read a file's contents with line numbers. Always read a file before
    editing it. `offset` is the 1-based start line; `limit` caps the lines
    returned (default 2000)."""
    return _read.execute(file_path=file_path, offset=offset, limit=limit)


@mcp.tool()
def grep(pattern: str, path: str = ".", include: str | None = None) -> str:
    """Search file contents with a regex. Returns matching lines as
    `path:lineno: line`. `path` may be a file or directory (default: cwd);
    `include` restricts the search to files matching a glob (e.g. '*.py')."""
    return _grep.execute(pattern=pattern, path=path, include=include)


if __name__ == "__main__":
    mcp.run(transport="stdio")
