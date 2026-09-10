"""Core agent loop.

This is the heart of CoreCoder.  The pattern is simple:

    user message -> LLM (with tools) -> tool calls? -> execute -> loop
                                      -> text reply? -> return to user

It keeps looping until the LLM responds with plain text (no tool calls),
which means it's done working and ready to report back.
"""

import hashlib
import json
import uuid

from .llm import LLM
from .runtime import ToolRuntime, ToolStatus
from .tools import default_tools
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
        workspace=None,
        runtime: ToolRuntime | None = None,
        ledger=None,
        run_id: str | None = None,
        artifact_dir=None,
        manage_run_lifecycle: bool = True,
        phase: str = "executor",
        agent_id: str | None = None,
        project_memory: str | None = None,
    ):
        self.llm = llm
        self.tools = tools if tools is not None else default_tools()
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
        prompt_cwd = str(workspace.root) if workspace is not None else None
        self._prompt_cwd = prompt_cwd
        self._project_memory = project_memory or ""
        self._system = system_prompt(
            self.tools,
            cwd=self._prompt_cwd,
            project_memory=self._project_memory,
        )

        # fast name->tool dispatch (covers MCP tools, which are NOT in the global
        # ALL_TOOLS registry get_tool() searches). Identical objects to before
        # when mcp is off, so the baseline dispatch path is behavior-preserving.
        self._tool_by_name = {t.name: t for t in self.tools}
        self.workspace = workspace
        if workspace is not None:
            for tool in self.tools:
                bind_workspace = getattr(tool, "bind_workspace", None)
                if bind_workspace is not None:
                    bind_workspace(workspace.root)
                fallback = getattr(tool, "_fallback", None)
                bind_fallback = getattr(fallback, "bind_workspace", None)
                if bind_fallback is not None:
                    bind_fallback(workspace.root)
        self.ledger = ledger
        self.phase = phase
        self.agent_id = agent_id or phase
        self.run_id = run_id or getattr(ledger, "run_id", None) or uuid.uuid4().hex
        self.runtime = runtime or ToolRuntime(
            self.tools,
            artifact_dir=artifact_dir,
            ledger=ledger,
            phase=phase,
            state_provider=(
                (lambda: workspace.snapshot().snapshot_hash)
                if workspace is not None
                else None
            ),
        )
        self.last_tool_observations = []
        self.last_finish_reason: str | None = None
        self._active_turn_id: str | None = None
        self._turn_counter = 0
        self._child_agent_counter = 0
        self._manage_run_lifecycle = manage_run_lifecycle

        # wire up sub-agent capability
        for t in self.tools:
            if isinstance(t, AgentTool):
                t._parent_agent = self

    def refresh_project_memory(self, project_memory: str | None) -> None:
        """Replace startup memory after an interactive /memory add command."""
        self._project_memory = project_memory or ""
        self._system = system_prompt(
            self.tools,
            cwd=self._prompt_cwd,
            project_memory=self._project_memory,
        )

    def _full_messages(self) -> list[dict]:
        return [{"role": "system", "content": self._system}] + self.messages

    def _tool_schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools]

    def chat(self, user_input: str, on_token=None, on_tool=None) -> str:
        """Process one user message. May involve multiple LLM/tool rounds."""
        self.last_tool_observations = []
        self.last_finish_reason = None
        self.runtime.reset_loop_guard()
        tool_round_pending = False
        failed_tool_round = False
        empty_tool_recoveries = 0
        baseline = getattr(self.workspace, "baseline_commit", None)
        root = str(self.workspace.root) if self.workspace is not None else None
        if self._manage_run_lifecycle:
            self._record("run_started", baseline_commit=baseline, workspace_root=root)
        try:
            self.messages.append({"role": "user", "content": user_input})
            self._maybe_compress(turn_id=None)

            for _ in range(self.max_rounds):
                self._turn_counter += 1
                turn_number = self._turn_counter
                self._active_turn_id = (
                    f"{self.run_id}:{self.agent_id}:turn:{turn_number}"
                )
                self._record(
                    "model_turn_started",
                    turn_id=self._active_turn_id,
                    turn_number=turn_number,
                )
                resp = self.llm.chat(
                    messages=self._full_messages(),
                    tools=self._tool_schemas(),
                    on_token=on_token,
                )
                self._record(
                    "model_turn_finished",
                    turn_id=self._active_turn_id,
                    turn_number=turn_number,
                    tool_call_count=len(resp.tool_calls),
                    prompt_tokens=resp.prompt_tokens,
                    completion_tokens=resp.completion_tokens,
                )

                # no tool calls -> LLM is done, return text
                if not resp.tool_calls:
                    self.messages.append(resp.message)
                    if not resp.content.strip() and tool_round_pending:
                        if empty_tool_recoveries < 2:
                            outcome = "failed" if failed_tool_round else "completed"
                            self.messages.append({
                                "role": "user",
                                "content": (
                                    f"[Runtime recovery] The previous tool round {outcome}, "
                                    "but your response was empty. Re-read the user's "
                                    "request and the Tool Observations. If work or "
                                    "verification remains, continue with the appropriate "
                                    "tools; otherwise provide a concise final result. "
                                    "Do not silently stop or claim success without "
                                    "verification."
                                ),
                            })
                            empty_tool_recoveries += 1
                            continue
                        self.last_finish_reason = "model_failure"
                        if self._manage_run_lifecycle:
                            self._record(
                                "run_finished",
                                result="model_failure",
                                reason="empty_response_after_tool",
                            )
                        return "(model repeatedly returned an empty response after tool calls)"
                    if self._manage_run_lifecycle:
                        self._record("run_finished", result="model_turn_complete")
                    return resp.content

                # tool calls -> execute through the effect-aware runtime
                self.messages.append(resp.message)
                tool_reply_start = len(self.messages)
                observation_start = len(self.last_tool_observations)

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

                self._record_workspace_snapshot()
                recent_observations = self.last_tool_observations[observation_start:]
                tool_round_pending = bool(recent_observations)
                failed_tool_round = any(
                    (item.original_status or item.status) is not ToolStatus.SUCCESS
                    for item in recent_observations
                )
                if any(
                    item.block_reason == "no_progress_stalled"
                    for item in recent_observations
                ):
                    self.last_finish_reason = "no_progress"
                    if self._manage_run_lifecycle:
                        self._record("run_finished", result="no_progress")
                    return "(stalled: repeated tool calls made no progress)"
                self._maybe_compress(turn_id=self._active_turn_id)

            if self._manage_run_lifecycle:
                self._record("run_finished", result="max_tool_rounds")
            self.last_finish_reason = "budget"
            return "(reached maximum tool-call rounds)"
        except KeyboardInterrupt:
            if self._manage_run_lifecycle:
                self._record("run_aborted", reason="cancelled", turn_id=self._active_turn_id)
            raise
        except BaseException as exc:
            if self._manage_run_lifecycle:
                self._record(
                    "run_aborted",
                    reason="exception",
                    error=f"{type(exc).__name__}: {exc}",
                    turn_id=self._active_turn_id,
                )
            raise
        finally:
            self._active_turn_id = None

    def _exec_tool(self, tc) -> str:
        """Execute one tool through the structured runtime."""
        observation = self.runtime.execute(tc, turn_id=self._active_turn_id)
        self.last_tool_observations.append(observation)
        if observation.status is ToolStatus.CANCELLED:
            raise KeyboardInterrupt
        return observation.model_text

    def _exec_tools_parallel(self, tool_calls, on_tool=None) -> list[str]:
        """Run multiple tool calls concurrently using threads.

        This is inspired by Claude Code's StreamingToolExecutor which starts
        executing tools while the model is still generating.  We simplify to:
        when the model returns N tool calls at once, run them in parallel.
        """
        for tc in tool_calls:
            if on_tool:
                on_tool(tc.name, tc.arguments)

        observations = self.runtime.execute_many(
            tool_calls, turn_id=self._active_turn_id
        )
        self.last_tool_observations.extend(observations)
        if any(item.status is ToolStatus.CANCELLED for item in observations):
            raise KeyboardInterrupt
        return [item.model_text for item in observations]

    def _record_workspace_snapshot(self) -> None:
        if self.ledger is None or self.workspace is None:
            return
        snapshot = self.workspace.snapshot()
        self._record(
            "workspace_snapshot",
            turn_id=self._active_turn_id,
            baseline_commit=snapshot.baseline_commit,
            snapshot_hash=snapshot.snapshot_hash,
            tracked_patch_sha256=snapshot.tracked_patch_sha256,
            tracked_patch_length=len(snapshot.tracked_patch),
            untracked_file_count=len(snapshot.untracked_files),
        )

    def _maybe_compress(self, *, turn_id: str | None) -> None:
        before = None
        if self.ledger is not None:
            before = json.dumps(self.messages, ensure_ascii=False, sort_keys=True)
        self.context.maybe_compress(self.messages, self.llm)
        if before is not None:
            after = json.dumps(self.messages, ensure_ascii=False, sort_keys=True)
            if after != before:
                self._record(
                    "compression",
                    turn_id=turn_id,
                    before_sha256=hashlib.sha256(before.encode("utf-8")).hexdigest(),
                    after_sha256=hashlib.sha256(after.encode("utf-8")).hexdigest(),
                )

    def _record(self, event_type: str, **payload) -> None:
        if self.ledger is not None:
            payload.setdefault("phase", self.phase)
            self.ledger.append(event_type, **payload)

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
