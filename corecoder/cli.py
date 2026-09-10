"""Interactive REPL - the user-facing terminal interface."""

import sys
import os
import argparse
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

from .agent import Agent
from .llm import LLM, LiteLLM
from .memory import ProjectMemoryStore, create_memory_from_skill
from .config import Config
from .session import save_session, load_session, list_sessions
from .workspace import WorkspaceError, WorkspaceExecution
from . import __version__

console = Console()


def _stream_token(token: str, stream=None) -> None:
    """Write one streamed token without crashing on console encoding limits."""
    output = stream or sys.stdout
    try:
        output.write(token)
    except UnicodeEncodeError:
        encoding = getattr(output, "encoding", None) or "utf-8"
        safe_token = token.encode(encoding, errors="replace").decode(encoding)
        output.write(safe_token)
    output.flush()


def _parse_args():
    p = argparse.ArgumentParser(
        prog="corecoder",
        description="Minimal AI coding agent. Works with any OpenAI-compatible LLM.",
    )
    p.add_argument("-m", "--model", help="Model name (default: $CORECODER_MODEL or mimo-v2.5)")
    p.add_argument("--base-url", help="API base URL (default: $OPENAI_BASE_URL)")
    p.add_argument("--api-key", help="API key (default: $OPENAI_API_KEY)")
    p.add_argument("-p", "--prompt", help="One-shot prompt (non-interactive mode)")
    p.add_argument("-r", "--resume", metavar="ID", help="Resume a saved session")
    memory = p.add_mutually_exclusive_group()
    memory.add_argument(
        "--memory-skill",
        metavar="TEXT",
        help="Summarize a manual memory skill with the configured model and save it",
    )
    memory.add_argument(
        "--memory-show",
        action="store_true",
        help="Print this project's persisted MEMORY.md",
    )
    memory.add_argument(
        "--memory-path",
        action="store_true",
        help="Print this project's memory directory",
    )
    memory.add_argument(
        "--memory-seed-demo",
        action="store_true",
        help="Create April-September 2026 demonstration memories",
    )
    p.add_argument(
        "--memory-date",
        metavar="YYYY-MM-DD",
        help="Date attached to --memory-skill (default: today)",
    )
    p.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args()


def main():
    args = _parse_args()
    config = Config.from_env()

    # CLI args override env vars
    if args.model:
        config.model = args.model
    if args.base_url:
        config.base_url = args.base_url
    if args.api_key:
        config.api_key = args.api_key

    memory_store = ProjectMemoryStore.for_project(Path.cwd())
    if args.memory_path:
        console.print(str(memory_store.directory))
        return
    if args.memory_show:
        content = memory_store.show()
        _print_memory_text(content)
        return
    if args.memory_seed_demo:
        count = memory_store.seed_demo()
        console.print(f"[green]Created {count} demo memories:[/green] {memory_store.demo_path}")
        return

    if not config.api_key:
        console.print("[red bold]No API key found.[/]")
        console.print(
            "Set one of: OPENAI_API_KEY, MIMO_API_KEY, DEEPSEEK_API_KEY, or CORECODER_API_KEY\n"
            "\nExamples:\n"
            "  # MiMo\n"
            "  export OPENAI_API_KEY=... OPENAI_BASE_URL=https://your-mimo-compatible-endpoint/v1 CORECODER_MODEL=mimo-v2.5\n"
            "\n"
            "  # OpenAI\n"
            "  export OPENAI_API_KEY=sk-...\n"
            "\n"
            "  # DeepSeek\n"
            "  export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com\n"
            "\n"
            "  # Ollama (local)\n"
            "  export OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1 CORECODER_MODEL=qwen2.5-coder\n"
        )
        sys.exit(1)

    llm_cls = LiteLLM if config.provider == "litellm" else LLM
    llm = llm_cls(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
    if args.memory_skill:
        try:
            result = create_memory_from_skill(
                llm,
                args.memory_skill,
                project_root=Path.cwd(),
                memory_date=args.memory_date,
            )
        except ValueError as exc:
            console.print(f"[red]Memory not saved: {exc}[/red]")
            sys.exit(2)
        mode = "model summary" if result.used_model_summary else "safe fallback summary"
        console.print(f"[green]Memory saved ({mode}):[/green] {result.path}")
        return
    try:
        workspace = WorkspaceExecution.resolve(Path.cwd())
    except WorkspaceError:
        workspace = None
    agent = Agent(
        llm=llm,
        max_context_tokens=config.max_context_tokens,
        workspace=workspace,
        project_memory=memory_store.load_startup(),
    )
    # create a agent with no tools registered by default;
    # users can add tools by creating a subclass and overriding the tools() method,
    # or by using the MCP tool registration API to add tools at runtime

    # resume saved session
    if args.resume:
        loaded = load_session(args.resume)
        if loaded:
            agent.messages, loaded_model = loaded
            # restore the model from the saved session unless overridden by CLI
            if not args.model:
                agent.llm.model = loaded_model
                config.model = loaded_model
            console.print(f"[green]Resumed session: {args.resume} (model: {agent.llm.model})[/green]")
        else:
            console.print(f"[red]Session '{args.resume}' not found.[/red]")
            sys.exit(1)

    # one-shot mode
    if args.prompt:
        _run_once(agent, args.prompt)
        return

    # interactive REPL
    _repl(agent, config, memory_store)


def _run_once(agent: Agent, prompt: str):
    """Non-interactive: run one prompt and exit."""
    def on_token(tok):
        _stream_token(tok)

    def on_tool(name, kwargs):
        console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

    agent.chat(prompt, on_token=on_token, on_tool=on_tool)
    print()


def _repl(agent: Agent, config: Config, memory_store: ProjectMemoryStore):
    """Interactive read-eval-print loop."""
    console.print(Panel(
        f"[bold]CoreCoder[/bold] v{__version__}\n"
        f"Model: [cyan]{config.model}[/cyan]"
        + (f"  Base: [dim]{config.base_url}[/dim]" if config.base_url else "")
        + "\nType [bold]/help[/bold] for commands, [bold]Ctrl+C[/bold] to cancel, [bold]quit[/bold] to exit.",
        border_style="blue",
    ))

    hist_path = os.path.expanduser("~/.corecoder_history")
    history = FileHistory(hist_path)

    # Enter submits, Escape+Enter inserts a newline (for pasting code blocks etc.)
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    while True:
        try:
            user_input = pt_prompt(
                "You > ",
                history=history,
                multiline=True,
                key_bindings=kb,
                prompt_continuation="...  ",
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye!")
            break

        if not user_input:
            continue

        # built-in commands
        if user_input.lower() in ("quit", "exit", "/quit", "/exit"):
            break
        if user_input == "/help":
            _show_help()
            continue
        if user_input == "/reset":
            agent.reset()
            console.print("[yellow]Conversation reset.[/yellow]")
            continue
        if user_input == "/tokens":
            p = agent.llm.total_prompt_tokens
            c = agent.llm.total_completion_tokens
            line = f"Tokens: [cyan]{p}[/cyan] prompt + [cyan]{c}[/cyan] completion = [bold]{p+c}[/bold] total"
            cost = agent.llm.estimated_cost
            if cost is not None:
                line += f"  (~¥{cost:.4f})"
            console.print(line)
            continue
        if user_input == "/model" or user_input.startswith("/model "):
            new_model = user_input[7:].strip() if user_input.startswith("/model ") else ""
            if new_model:
                agent.llm.model = new_model
                config.model = new_model
                console.print(f"Switched to [cyan]{new_model}[/cyan]")
            else:
                console.print(f"Current model: [cyan]{config.model}[/cyan]")
            continue
        if user_input == "/compact":
            from .context import estimate_tokens
            before = estimate_tokens(agent.messages)
            compressed = agent.context.maybe_compress(agent.messages, agent.llm)
            after = estimate_tokens(agent.messages)
            if compressed:
                console.print(f"[green]Compressed: {before} → {after} tokens ({len(agent.messages)} messages)[/green]")
            else:
                console.print(f"[dim]Nothing to compress ({before} tokens, {len(agent.messages)} messages)[/dim]")
            continue
        if user_input == "/save":
            sid = save_session(agent.messages, config.model)
            console.print(f"[green]Session saved: {sid}[/green]")
            console.print(f"Resume with: corecoder -r {sid}")
            continue
        if user_input == "/diff":
            from .tools.edit import _changed_files
            if not _changed_files:
                console.print("[dim]No files modified this session.[/dim]")
            else:
                console.print(f"[bold]Files modified this session ({len(_changed_files)}):[/bold]")
                for f in sorted(_changed_files):
                    console.print(f"  [cyan]{f}[/cyan]")
            continue
        if user_input == "/sessions":
            sessions = list_sessions()
            if not sessions:
                console.print("[dim]No saved sessions.[/dim]")
            else:
                for s in sessions:
                    console.print(f"  [cyan]{s['id']}[/cyan] ({s['model']}, {s['saved_at']}) {s['preview']}")
            continue
        if user_input == "/memory" or user_input.startswith("/memory "):
            _handle_memory_command(agent, memory_store, user_input)
            continue

        # call the agent
        streamed: list[str] = []

        def on_token(tok):
            streamed.append(tok)
            _stream_token(tok)

        def on_tool(name, kwargs):
            console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

        try:
            response = agent.chat(user_input, on_token=on_token, on_tool=on_tool)
            if streamed:
                print()  # newline after streamed tokens
            else:
                # response wasn't streamed (came after tool calls)
                console.print(Markdown(response))
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
        except Exception as e:
            console.print(f"\n[red]Error: {e}[/red]")


def _show_help():
    console.print(Panel(
        "[bold]Commands:[/bold]\n"
        "  /help          Show this help\n"
        "  /reset         Clear conversation history\n"
        "  /model         Show current model\n"
        "  /model <name>  Switch model mid-conversation\n"
        "  /tokens        Show token usage\n"
        "  /compact       Compress conversation context\n"
        "  /diff          Show files modified this session\n"
        "  /save          Save session to disk\n"
        "  /sessions      List saved sessions\n"
        "  /memory show   Show project memory\n"
        "  /memory path   Show the project memory directory\n"
        "  /memory add <skill>  Summarize and save a manual memory skill\n"
        "  /memory add YYYY-MM-DD :: <skill>  Save with an explicit date\n"
        "  /memory demo   Create and show demonstration memory\n"
        "  quit           Exit CoreCoder\n"
        "\n"
        "[bold]Input:[/bold]\n"
        "  Enter          Submit message\n"
        "  Esc+Enter      Insert newline (for pasting code)",
        title="CoreCoder Help",
        border_style="dim",
    ))


def _handle_memory_command(
    agent: Agent,
    store: ProjectMemoryStore,
    command: str,
) -> None:
    action = command[len("/memory") :].strip()
    if action in ("", "show"):
        content = store.show()
        _print_memory_text(content)
        return
    if action == "path":
        console.print(str(store.directory))
        return
    if action == "demo":
        count = store.seed_demo()
        console.print(f"[green]Created {count} demo memories:[/green] {store.demo_path}")
        _print_memory_text(store.show(include_demo=True))
        return
    if not action.startswith("add "):
        console.print(
            "[yellow]Usage: /memory show | path | demo | add [YYYY-MM-DD ::] <memory skill>[/yellow]"
        )
        return

    payload = action[4:].strip()
    memory_date = None
    possible_date, separator, remainder = payload.partition("::")
    if separator and len(possible_date.strip()) == 10:
        memory_date = possible_date.strip()
        payload = remainder.strip()
    try:
        result = create_memory_from_skill(
            agent.llm,
            payload,
            project_root=store.project_root,
            memory_date=memory_date,
            home=store.directory.parent.parent,
        )
    except ValueError as exc:
        console.print(f"[red]Memory not saved: {exc}[/red]")
        return

    agent.refresh_project_memory(store.load_startup())
    mode = "model summary" if result.used_model_summary else "safe fallback summary"
    console.print(f"[green]Memory saved ({mode}):[/green] {result.path}")


def _print_memory_text(content: str, stream=None) -> None:
    """Print persisted Markdown without Rich's non-GBK bullet rendering."""
    if not content:
        console.print("[dim]No project memory yet.[/dim]")
        return
    _stream_token(content.rstrip() + "\n", stream=stream)


def _brief(kwargs: dict, maxlen: int = 80) -> str:
    s = ", ".join(f"{k}={repr(v)[:40]}" for k, v in kwargs.items())
    return s[:maxlen] + ("..." if len(s) > maxlen else "")
