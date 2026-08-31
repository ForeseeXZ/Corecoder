"""Core agent loop.

This is the heart of CoreCoder.  The pattern is simple:

    user message -> LLM (with tools) -> tool calls? -> execute -> loop
                                      -> text reply? -> return to user

It keeps looping until the LLM responds with plain text (no tool calls),
which means it's done working and ready to report back.
"""

import concurrent.futures
import inspect
from .llm import LLM
from .tools import ALL_TOOLS
from .tools.base import Tool
from .tools.agent import AgentTool
from .prompt import system_prompt
from .context import ContextManager


class Agent:
    def __init__(
        self,
        llm: LLM,
        tools: list[Tool] | None = None,
        max_context_tokens: int = 128_000,
        max_rounds: int = 50,
        compress: bool = False,
        compress_cfg: dict | None = None,
        mcp: bool = False,
        mcp_cfg: dict | None = None,
    ):
        self.llm = llm
        self.tools = tools if tools is not None else ALL_TOOLS
        self.messages: list[dict] = []

        # MCP: default OFF -> the in-process tools above are used unchanged
        # (baseline). With mcp=True, start a standalone MCP server, DYNAMICALLY
        # DISCOVER its tools, and swap the matching builtins (read_file/grep) for
        # their MCP-backed twins. Done BEFORE system_prompt() so the agent's
        # system message reflects the discovered tool surface. On any startup
        # failure setup_mcp returns the builtins unchanged (full fallback).
        self._mcp_bridge = None
        self.mcp_stats = None
        if mcp:
            from .mcp_bridge import setup_mcp
            cfg = mcp_cfg or {}
            self.tools, self._mcp_bridge, self.mcp_stats = setup_mcp(
                self.tools,
                repo_dir=cfg.get("cwd"),
                call_timeout=cfg.get("call_timeout", 60.0),
                startup_timeout=cfg.get("startup_timeout", 30.0),
            )

        # Context manager: default OFF -> the basic always-on ContextManager
        # (the measured baseline path, unchanged). With compress=True, swap in the
        # instrumented multi-layer CompressionManager (same maybe_compress surface
        # + a .stats dict the runner folds into the summary). Mirrors how the
        # Planner/Reviewer features are bolted on without disturbing baseline.
        if compress:
            from .compress import CompressionManager
            self.context = CompressionManager(
                max_tokens=max_context_tokens, **(compress_cfg or {})
            )
        else:
            self.context = ContextManager(max_tokens=max_context_tokens)
        self.max_rounds = max_rounds
        self._system = system_prompt(self.tools)

        # fast name->tool dispatch (covers MCP tools, which are NOT in the global
        # ALL_TOOLS registry get_tool() searches). Identical objects to before
        # when mcp is off, so the baseline dispatch path is behavior-preserving.
        self._tool_by_name = {t.name: t for t in self.tools}

        # wire up sub-agent capability
        for t in self.tools:
            if isinstance(t, AgentTool):
                t._parent_agent = self

    def _full_messages(self) -> list[dict]:
        return [{"role": "system", "content": self._system}] + self.messages

    def _tool_schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools]

    def chat(self, user_input: str, on_token=None, on_tool=None) -> str:
        """Process one user message. May involve multiple LLM/tool rounds."""
        self.messages.append({"role": "user", "content": user_input})
        self.context.maybe_compress(self.messages, self.llm)

        for _ in range(self.max_rounds):
            resp = self.llm.chat(
                messages=self._full_messages(),
                tools=self._tool_schemas(),
                on_token=on_token,
            )

            # no tool calls -> LLM is done, return text
            if not resp.tool_calls:
                self.messages.append(resp.message)
                return resp.content

            # tool calls -> execute (parallel when multiple, like Claude Code's
            # StreamingToolExecutor which runs independent tools concurrently)
            self.messages.append(resp.message)
            tool_reply_start = len(self.messages)

            try:
                if len(resp.tool_calls) == 1:
                    tc = resp.tool_calls[0]
                    if on_tool:
                        on_tool(tc.name, tc.arguments)
                    result = self._exec_tool(tc)
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                else:
                    # parallel execution for multiple tool calls
                    results = self._exec_tools_parallel(resp.tool_calls, on_tool)
                    for tc, result in zip(resp.tool_calls, results):
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result,
                        })
            except KeyboardInterrupt:
                completed_ids = {
                    message.get("tool_call_id")
                    for message in self.messages[tool_reply_start:]
                    if message.get("role") == "tool"
                }
                for tc in resp.tool_calls:
                    if tc.id not in completed_ids:
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": "[interrupted]",
                        })
                raise

            # compress if tool outputs are big
            self.context.maybe_compress(self.messages, self.llm)

        return "(reached maximum tool-call rounds)"

    def _exec_tool(self, tc) -> str:
        """Execute a single tool call, returning the result string."""
        tool = self._tool_by_name.get(tc.name)
        if tool is None:
            return f"Error: unknown tool '{tc.name}'"
        parameters = tool.parameters or {}
        required = set(parameters.get("required", []))
        provided = set(tc.arguments)
        missing = sorted(required - provided)
        unexpected = sorted(provided - set(parameters.get("properties", {})))
        if missing:
            return f"Error: bad arguments for {tc.name}: missing {', '.join(missing)}"
        if unexpected:
            return (
                f"Error: bad arguments for {tc.name}: "
                f"unexpected {', '.join(unexpected)}"
            )
        try:
            inspect.signature(tool.execute).bind(**tc.arguments)
        except TypeError as e:
            return f"Error: bad arguments for {tc.name}: {e}"
        try:
            return tool.execute(**tc.arguments)
        except Exception as e:
            return f"Error executing {tc.name}: {e}"

    def _exec_tools_parallel(self, tool_calls, on_tool=None) -> list[str]:
        """Run multiple tool calls concurrently using threads.

        This is inspired by Claude Code's StreamingToolExecutor which starts
        executing tools while the model is still generating.  We simplify to:
        when the model returns N tool calls at once, run them in parallel.
        """
        for tc in tool_calls:
            if on_tool:
                on_tool(tc.name, tc.arguments)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self._exec_tool, tc) for tc in tool_calls]
            return [f.result() for f in futures]

    def reset(self):
        """Clear conversation history."""
        self.messages.clear()

    def close(self):
        """Release external resources. Shuts down the MCP server subprocess (if
        any) so no zombie process is left behind. Safe to call multiple times and
        a no-op when MCP is off. The runner calls this in its finally block."""
        if self._mcp_bridge is not None:
            try:
                self._mcp_bridge.close()
            finally:
                self._mcp_bridge = None
