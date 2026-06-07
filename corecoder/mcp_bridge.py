"""Async/sync bridge that lets the synchronous agent loop drive an MCP server.

The agent loop (`Agent.chat`) is fully synchronous: it calls `tool.execute(**kw)`
and expects a string back. The MCP Python client is fully asynchronous
(`await session.call_tool(...)`). This module bridges the two and implements the
"agent as MCP client with dynamic tool discovery" half of the increment.

How the bridge works
--------------------
`McpBridge` owns a dedicated background thread running its own asyncio event
loop. On that loop a single long-lived coroutine (`_serve`):
  1. spawns the MCP server as a subprocess over stdio (cwd = the repo checkout,
     so relative paths resolve exactly as the in-process tools would),
  2. opens ONE persistent `ClientSession`, runs the MCP `initialize` handshake,
  3. calls `list_tools()` to DYNAMICALLY DISCOVER what the server exposes,
  4. then parks on a shutdown `asyncio.Event`, keeping the session alive.
The session stays open for the whole agent run, so we pay the spawn+handshake
cost exactly once (not per tool call).

A synchronous `call_sync(name, args)` hands the coroutine to that loop via
`asyncio.run_coroutine_threadsafe(...).result(timeout)` — the only safe way to
call into an event loop running on another thread — and blocks for the result.

Dynamic tool discovery
-----------------------
`setup_mcp()` calls `list_tools()` and builds one `McpTool` per discovered tool,
using the server-provided JSON-Schema as the tool's `parameters`. The agent loads
THOSE schemas into the LLM prompt — it learns the tool surface from the server at
runtime rather than from a hardcoded in-process list. Builtin tools whose name the
server also provides (read_file, grep) are replaced by their MCP-backed twins;
every other builtin (bash/edit/write/glob/agent) is kept untouched.

Graceful degradation (never fail a solvable task because of MCP)
----------------------------------------------------------------
  - If the server won't start (`bridge.start()` raises within `startup_timeout`),
    `setup_mcp()` returns the ORIGINAL builtin tools unchanged and flags
    `fallback=True` — the agent runs entirely on in-process tools.
  - If an individual `call_sync` raises or times out, `McpTool.execute` falls back
    to the matching builtin tool's `execute` and records the fallback.
All of this is captured in a `stats` dict the runner folds into `summary["mcp"]`.

No zombies
----------
Shutdown sets the `asyncio.Event`, which lets `_serve` return and exit the
`async with stdio_client(...)` / `ClientSession(...)` blocks. Their `__aexit__`
terminates the server subprocess. `close()` is invoked from the agent's
`finally` path so the subprocess is reaped even if the run errors out.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

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
        self._session = None                 # mcp.ClientSession, set on the loop
        self._shutdown: asyncio.Event | None = None   # created on the loop
        self._ready = threading.Event()      # set (loop thread) when init done OR failed
        self._start_error: BaseException | None = None
        self.tools: list = []                # discovered mcp.types.Tool objects

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> list:
        """Launch the loop thread + server subprocess; block until the session is
        initialized and tools are discovered. Returns the discovered tool list.
        Raises if the server fails to come up within `startup_timeout`."""
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
        except Exception as e:                      # loop-creation failure
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
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", self._server_module],
            cwd=self.cwd,
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self._session = session
                    self.tools = listed.tools
                    self._ready.set()               # hand control back to start()
                    await self._shutdown.wait()      # park until close()
            # both context managers exited cleanly -> subprocess terminated
        except Exception as e:
            # failure either during startup (ready not yet set) or mid-run;
            # set ready so start() unblocks and sees the error.
            self._start_error = e
            self._ready.set()

    def close(self, timeout: float = 10.0):
        """Signal shutdown and join the loop thread. Idempotent. Exiting the
        session context managers terminates the server subprocess (no zombies)."""
        loop, shutdown = self._loop, self._shutdown
        if loop is not None and shutdown is not None:
            try:
                loop.call_soon_threadsafe(shutdown.set)
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    # -- the sync entry point the tools call -------------------------------- #
    def call_sync(self, name: str, arguments: dict, timeout: float = 60.0) -> str:
        """Run `session.call_tool(name, arguments)` on the background loop and
        block for its text result. Raises on transport/timeout/tool error so the
        caller (McpTool) can fall back to the builtin."""
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
    """A Tool whose schema was discovered from the MCP server and whose execution
    is routed through the bridge — with automatic fallback to a builtin twin."""

    def __init__(self, name, description, parameters, bridge, fallback,
                 stats, call_timeout=60.0):
        self.name = name
        self.description = description
        self.parameters = parameters
        self._bridge = bridge
        self._fallback = fallback          # builtin Tool with the same name, or None
        self._stats = stats
        self._timeout = call_timeout

    def execute(self, **kwargs) -> str:
        try:
            out = self._bridge.call_sync(self.name, kwargs, timeout=self._timeout)
            self._stats["tool_calls"] += 1
            return out
        except Exception as e:                       # noqa: BLE001 — degrade, never crash
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
        "tool_calls": 0,        # calls served by the MCP server
        "fallback_calls": 0,    # calls that fell back to a builtin
        "fallback": False,      # True if startup OR any call fell back
        "error": None,
    }


def setup_mcp(
    builtin_tools: list[Tool],
    repo_dir: str | None = None,
    *,
    call_timeout: float = 60.0,
    startup_timeout: float = 30.0,
) -> tuple[list[Tool], McpBridge | None, dict]:
    """Start the MCP server, discover its tools, and return the tool list the
    agent should use. On any startup failure, returns the builtins unchanged
    (full fallback). Returns (tools, bridge_or_None, stats)."""
    stats = new_stats()
    builtin_by_name = {t.name: t for t in builtin_tools}

    bridge = McpBridge(cwd=repo_dir, startup_timeout=startup_timeout)
    t0 = time.monotonic()
    try:
        discovered = bridge.start()
    except Exception as e:                           # server won't come up
        stats["error"] = f"{type(e).__name__}: {e}"
        stats["fallback"] = True
        try:
            bridge.close()
        except Exception:
            pass
        return list(builtin_tools), None, stats      # agent runs fully on builtins

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
    # keep every builtin the server does NOT provide; swap in the MCP twins
    kept = [t for t in builtin_tools if t.name not in mcp_names]
    return kept + mcp_tools, bridge, stats
