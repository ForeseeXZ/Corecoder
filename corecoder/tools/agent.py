"""Sub-agent spawning (inspired by Claude Code's AgentTool, 1397 lines).

The idea: for complex sub-tasks, spawn an independent agent with its own
conversation history and tool access. This lets the main agent delegate
work like "go research this codebase and report back" without polluting
its own context window.

The sub-agent runs to completion and returns a text summary.
"""

from .base import Tool, ToolEffect


class AgentTool(Tool):
    name = "agent"
    description = (
        "Spawn a sub-agent to handle a complex sub-task independently. "
        "The sub-agent has its own context and tool access. Use this for: "
        "researching a codebase, implementing a multi-step change in isolation, "
        "or any task that would benefit from a fresh context window."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "What the sub-agent should accomplish",
            },
        },
        "required": ["task"],
    }
    effect = ToolEffect.PROCESS

    # set by Agent.__init__ after construction
    _parent_agent = None

    def execute(self, task: str) -> str:
        if self._parent_agent is None:
            return "Error: agent tool not initialized (no parent agent)"

        # import here to avoid circular dep
        from ..agent import Agent

        parent = self._parent_agent
        parent._child_agent_counter += 1
        sub = Agent(
            llm=parent.llm,
            tools=[t for t in parent.tools if t.name != "agent"],  # no recursive agents
            max_context_tokens=parent.context.max_tokens,
            max_rounds=20,
            workspace=getattr(parent, "workspace", None),
            ledger=getattr(parent, "ledger", None),
            run_id=getattr(parent, "run_id", None),
            artifact_dir=getattr(getattr(parent, "runtime", None), "artifact_dir", None),
            manage_run_lifecycle=False,
            phase="sub_agent",
            agent_id=f"{parent.agent_id}.sub{parent._child_agent_counter}",
            project_memory=getattr(parent, "_project_memory", ""),
        )

        try:
            result = sub.chat(task)
            # trim long results to avoid blowing up parent's context
            if len(result) > 5000:
                result = result[:4500] + "\n... (sub-agent output truncated)"
            return f"[Sub-agent completed]\n{result}"
        except Exception as e:
            return f"Sub-agent error: {e}"
