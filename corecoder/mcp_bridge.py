"""Async/sync bridge that lets the synchronous agent loop drive an MCP server.

The agent loop (`Agent.chat`) is fully synchronous: it calls `tool.execute(**kw)`
and expects a string back. The MCP Python client is fully asynchronous
(`await session.call_tool(...)`). This module bridges the two and implements the
"agent as MCP client with dynamic tool discovery" half of the increment.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

from .tools.base import Tool


class McpBridge:
    """Owns a background event loop + one persistent stdio MCP client session."""

    def __init__(
        self,
        cwd: str | None = None,
        *,
        startup_timeout: float = 30.0,
        server_module: str = "corecoder.mcp_server",
    ):
        self.cwd = cwd or os.getcwd()
        self.startup_timeout = startup_timeout
        self._server_module = server_module

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session = None
        self._shutdown: asyncio.Event | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self.tools: list = []

    def start(self) -> list:
        """Launch the loop thread + server subprocess, then discover tools."""
        self._thread = threading.Thread(
            target=self._run_loop, name="mcp-bridge", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=self.startup_timeout):
            self._start_error = self._start_error or TimeoutError(
                f"MCP server not ready within {self.startup_timeout}s"
            )
        if self._start_error is not None:
            raise RuntimeError(f"MCP bridge failed to start: {self._start_error}")
        return self.tools

    def _run_loop(self):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._serve())
        except Exception as e:
            self._start_error = e
            self._ready.set()
        finally:
            try:
                if self._loop is not None:
                    self._loop.close()
            except Exception:
                pass

    async def _serve(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._shutdown = asyncio.Event()
        env = dict(os.environ)
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = (
            package_root
            + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", self._server_module],
            cwd=self.cwd,
            env=env,
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self._session = session
                    self.tools = listed.tools
                    self._ready.set()
                    await self._shutdown.wait()
        except Exception as e:
            self._start_error = e
            self._ready.set()

    def close(self, timeout: float = 10.0):
        """Signal shutdown and join the loop thread."""
        loop, shutdown = self._loop, self._shutdown
        if loop is not None and shutdown is not None:
            try:
                loop.call_soon_threadsafe(shutdown.set)
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def call_sync(self, name: str, arguments: dict, timeout: float = 60.0) -> str:
        """Call an MCP tool from synchronous tool code."""
        if self._loop is None or self._session is None:
            raise RuntimeError("MCP bridge not running")
        coro = self._session.call_tool(name, arguments)
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        result = fut.result(timeout=timeout)
        if getattr(result, "isError", False):
            raise RuntimeError(f"MCP tool '{name}' reported an error")
        parts: list[str] = []
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
        return "\n".join(parts)


class McpTool(Tool):
    """A Tool discovered from MCP with fallback to a builtin twin."""

    def __init__(self, name, description, parameters, bridge, fallback,
                 stats, call_timeout=60.0):
        self.name = name
        self.description = description
        self.parameters = parameters
        self._bridge = bridge
        self._fallback = fallback
        self._stats = stats
        self._timeout = call_timeout

    def execute(self, **kwargs) -> str:
        try:
            out = self._bridge.call_sync(self.name, kwargs, timeout=self._timeout)
            self._stats["tool_calls"] += 1
            return out
        except Exception as e:
            self._stats["fallback_calls"] += 1
            self._stats["fallback"] = True
            if self._fallback is not None:
                try:
                    return self._fallback.execute(**kwargs)
                except Exception as fe:
                    return (f"Error: MCP tool '{self.name}' failed ({e}); "
                            f"builtin fallback also failed: {fe}")
            return f"Error: MCP tool '{self.name}' failed and no fallback exists: {e}"


def new_stats() -> dict:
    return {
        "enabled": True,
        "server_started": False,
        "startup_s": None,
        "discovered_tools": [],
        "replaced_builtins": [],
        "tool_calls": 0,
        "fallback_calls": 0,
        "fallback": False,
        "error": None,
    }


def setup_mcp(
    builtin_tools: list[Tool],
    repo_dir: str | None = None,
    *,
    call_timeout: float = 60.0,
    startup_timeout: float = 30.0,
) -> tuple[list[Tool], McpBridge | None, dict]:
    """Start the MCP server, discover tools, and return the agent tool list."""
    stats = new_stats()
    builtin_by_name = {t.name: t for t in builtin_tools}

    bridge = McpBridge(cwd=repo_dir, startup_timeout=startup_timeout)
    t0 = time.monotonic()
    try:
        discovered = bridge.start()
    except Exception as e:
        stats["error"] = f"{type(e).__name__}: {e}"
        stats["fallback"] = True
        try:
            bridge.close()
        except Exception:
            pass
        return list(builtin_tools), None, stats

    stats["server_started"] = True
    stats["startup_s"] = round(time.monotonic() - t0, 3)
    stats["discovered_tools"] = [t.name for t in discovered]

    mcp_tools: list[Tool] = []
    mcp_names: set[str] = set()
    for mt in discovered:
        params = getattr(mt, "inputSchema", None) or {"type": "object", "properties": {}}
        mcp_tools.append(McpTool(
            name=mt.name,
            description=mt.description or "",
            parameters=params,
            bridge=bridge,
            fallback=builtin_by_name.get(mt.name),
            stats=stats,
            call_timeout=call_timeout,
        ))
        mcp_names.add(mt.name)

    stats["replaced_builtins"] = [n for n in builtin_by_name if n in mcp_names]
    kept = [t for t in builtin_tools if t.name not in mcp_names]
    return kept + mcp_tools, bridge, stats
