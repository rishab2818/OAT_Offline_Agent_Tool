"""Command-line interface and interactive session."""
import argparse
import json
import shutil
import sys
import threading
import time
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path

from .agent import Agent, AgentError
from .config import Config
from .command_tools import register_command_tools
from .file_tools import register_file_tools
from .ollama import OllamaClient, OllamaError
from .preferences import load_preferences, setup_wizard
from .registry import Registry
from .storage import RunLog, private_directory, workspace_lock
from .workspace import Workspace, ToolError
from .trace import display_trace
from .tasks import TaskManager, CONTROL_TOOLS
from .workflow_contract import workflow_contract
from .ui import TerminalUI
from . import __version__


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.json"


class WorkspaceSwitch(Exception):
    def __init__(self, path):
        self.path = str(Path(path).resolve())


class ModelSwitch(Exception):
    def __init__(self, model):
        self.model = model


class ReloadSession(Exception):
    pass


def configure_console_output():
    """Prevent model Unicode from crashing legacy Windows console encodings."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


def workspace_argv(argv, path):
    result, skip = [], False
    for value in argv:
        if skip:
            skip = False
            continue
        if value == "--workspace":
            skip = True
            continue
        if value.startswith("--workspace="):
            continue
        result.append(value)
    return [*result, "--workspace", str(path)]


def model_argv(argv, model):
    result, skip = [], False
    for value in argv:
        if skip:
            skip = False
            continue
        if value == "--model":
            skip = True
            continue
        if value.startswith("--model="):
            continue
        result.append(value)
    return [*result, "--model", model]


def reload_argv(argv):
    return [value for value in argv if value != "--setup"]


def clear_saved_data(directory, scope, current_log=None, current_task=None):
    """Delete only old agent-owned records under the exact private directory."""
    directory = directory.resolve()
    removed = {"logs": 0, "tasks": 0}
    if scope in {"logs", "all"}:
        for path in directory.glob("run-*.jsonl"):
            if current_log and path.resolve() == Path(current_log).resolve():
                continue
            path.unlink()
            removed["logs"] += 1
    if scope in {"tasks", "all"}:
        task_root = (directory / "tasks").resolve()
        if task_root.is_dir() and task_root.parent == directory:
            for path in task_root.iterdir():
                if not path.is_dir() or path.is_symlink():
                    continue
                if current_task and path.resolve() == Path(current_task).resolve():
                    continue
                if path.resolve().parent != task_root:
                    raise ToolError("Refusing to clear an unexpected task path")
                shutil.rmtree(path)
                removed["tasks"] += 1
    return removed


def parser():
    result = argparse.ArgumentParser(description="Small local Ollama agent for text files, code and shell commands.")
    result.add_argument("--version", action="version", version="local-agent " + __version__)
    result.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    result.add_argument("--workspace", help="Default working directory; relative CLI paths resolve from the current directory")
    result.add_argument("--model", help="Ollama model tag, default gpt-oss:20b")
    result.add_argument("--base-url", help="Ollama HTTP server URL")
    prompts = result.add_mutually_exclusive_group()
    prompts.add_argument("--prompt", "-p", help="Run one prompt and exit")
    prompts.add_argument("--prompt-file", type=Path, help="Read a multiline prompt from a UTF-8 file")
    result.add_argument("--agent-file", help="Optional instruction file, relative to workspace or absolute")
    result.add_argument("--instructions", action="append", help="Additional instruction file; repeat for PLAN.md, SKILL.md, etc.")
    result.add_argument("--resume", nargs="?", const="latest", help="Resume a saved task ID, or the latest task")
    result.add_argument("--tool-mode", choices=("auto", "native", "json"))
    result.add_argument("--trace", action=argparse.BooleanOptionalAction, default=None,
                        help="Show complete model requests/responses and tool calls/results (default on)")
    result.add_argument("--think", choices=("low", "medium", "high", "true", "false", "default"))
    result.add_argument("--max-steps", type=int)
    result.add_argument("--timeout-seconds", type=int)
    result.add_argument("--doctor", action="store_true", help="Check configuration, workspace and local model; do not run analysis")
    result.add_argument("--setup", action="store_true", help="Run the first-run setup wizard")
    result.add_argument("--ui-mode", choices=("compact", "detailed"), help="Terminal interface verbosity")
    return result


def selected_instruction_documents(config, workspace):
    documents = []
    for relative in config.instruction_files:
        path = (workspace.root / relative).resolve()
        documents.append((path, path.read_text(encoding="utf-8-sig")))
    return documents


def load_instructions(config, workspace):
    chunks = []
    if config.system_prompt:
        chunks.append(Path(config.system_prompt).read_text(encoding="utf-8-sig"))
    for instruction_path, content in selected_instruction_documents(config, workspace):
        if len(content) > config.max_file_chars:
            raise ToolError(f"Instruction file exceeds size limit: {instruction_path}")
        chunks.append(f"\nUser-selected task instructions ({instruction_path}):\n{content}")
    agent_path = None
    if config.agent_file:
        agent_path = (workspace.root / config.agent_file).resolve()
    else:
        for name in ("agent.md", "AGENTS.md"):
            candidate = workspace.root / name
            if candidate.is_file():
                agent_path = candidate
                break
    if agent_path:
        content = agent_path.read_text(encoding="utf-8-sig")
        if len(content) > config.max_file_chars:
            raise ToolError("Optional agent instruction file exceeds size limit")
        chunks.append(f"\nAdditional project instructions ({agent_path.name}):\n{content}")
    chunks.append(f"\nWorkspace: {workspace.root}\nFile access: {config.file_access}")
    return "\n".join(chunks)


@contextmanager
def heartbeat(output=print):
    output("Waiting for the local model/tool response... Ctrl+C stops safely.")
    try:
        yield
    finally:
        pass


def _main(argv=None):
    configure_console_output()
    args = parser().parse_args(argv)
    try:
        preferences = load_preferences()
        overrides = {key: getattr(args, key) for key in
                     ("model", "base_url", "tool_mode", "agent_file", "max_steps", "timeout_seconds", "trace")}
        if not args.model and preferences.get("model"):
            overrides["model"] = preferences["model"]
        if not args.timeout_seconds and preferences.get("timeout_seconds"):
            overrides["timeout_seconds"] = preferences["timeout_seconds"]
        if args.workspace:
            overrides["workspace"] = str(Path(args.workspace).resolve())
        elif preferences.get("workspace"):
            overrides["workspace"] = preferences["workspace"]
        config = Config.load(args.config, overrides)
        if preferences.get("context_tokens"):
            config.options["num_ctx"] = preferences["context_tokens"]
        if args.instructions:
            config.instruction_files.extend(args.instructions)
        if args.think is not None:
            config.think = {"true": True, "false": False, "default": None}.get(args.think, args.think)
        workspace = Workspace(config.workspace, config.max_file_chars)
        instructions = load_instructions(config, workspace)
        client = OllamaClient(config.base_url, config.timeout_seconds)
        first_run = (not preferences.get("setup_complete") and sys.stdin.isatty()
                     and args.prompt is None and args.prompt_file is None and not args.resume and not args.doctor)
        if args.setup or first_run:
            setup_wizard(client, config)
            raise ReloadSession()
        ui = TerminalUI(args.ui_mode or preferences.get("ui_mode", "compact"))
        if args.doctor:
            models = client.models()
            available = config.model in models or config.model + ":latest" in models
            print(json.dumps({"workspace": config.workspace, "enabled_tools": config.enabled_tools,
                              "ollama": config.base_url, "model": config.model,
                              "model_installed": available, "installed_models": models,
                              "instructions_loaded": True}, indent=2))
            return 0 if available else 1
        directory = private_directory(workspace)
        with workspace_lock(directory):
            log = RunLog(directory)
            def trace_traffic(event, data):
                log.write("ollama_" + event, data=data)
                if config.trace:
                    label = {"request": "REQUEST -> OLLAMA", "response": "RESPONSE <- OLLAMA",
                             "error": "OLLAMA ERROR"}[event]
                    display_trace(ui.write, label, data)
            client.trace = trace_traffic
            registry = Registry()
            register_file_tools(registry, workspace, config)
            register_command_tools(registry, workspace, config)
            requirements = workflow_contract(selected_instruction_documents(config, workspace))
            tasks = TaskManager(workspace, directory, config.enabled_tools, requirements,
                                direct_mode=not requirements,
                                metadata={"model": config.model, "context_tokens": config.options.get("num_ctx"),
                                          "tool_mode": config.tool_mode})
            tasks.register(registry)
            registry.select(list(config.enabled_tools) + sorted(CONTROL_TOOLS))
            ui.banner(__version__, config, log.path)
            log.write("session", runtime_version=__version__, config=vars(config), instructions=instructions)
            agent = Agent(client, registry, config, instructions, log, tasks=tasks, emit=ui.emit)
            def run(prompt, resume=False):
                if not prompt.strip():
                    raise ValueError("Prompt cannot be empty")
                try:
                    ui.task_start(prompt)
                    with heartbeat(ui.write):
                        answer = agent.run(prompt, resume=resume)
                except KeyboardInterrupt:
                    tasks.block("Interrupted by user. Completed steps and tool evidence were saved.")
                    raise
                except Exception as exc:
                    tasks.block(str(exc))
                    log.write("error", error=str(exc))
                    raise
                ui.task_complete(answer, tasks.review())

            def interactive_run(prompt, resume=False):
                """Run work in the background so /stop remains available on Windows."""
                if sys.platform != "win32" or not sys.stdin.isatty():
                    return run(prompt, resume)
                import msvcrt
                outcome = {}
                def worker():
                    try:
                        run(prompt, resume)
                    except BaseException as exc:
                        outcome["error"] = exc
                thread = threading.Thread(target=worker, daemon=True, name="agent-task")
                thread.start()
                ui.write("Controls while working: /status  /artifacts  /stop")
                line = ""
                while thread.is_alive():
                    if not msvcrt.kbhit():
                        time.sleep(0.05)
                        continue
                    char = msvcrt.getwch()
                    if char in ("\r", "\n"):
                        print()
                        command, line = line.strip(), ""
                        if command == "/stop":
                            print("[control] Pausing after the current atomic tool action...", flush=True)
                            agent.cancel()
                        elif command in {"/plan", "/status"}:
                            ui.show_json(tasks.status())
                        elif command == "/artifacts":
                            ui.show_json(tasks.review())
                        elif command:
                            ui.write("While working, use /status, /artifacts, or /stop.")
                    elif char == "\b":
                        if line:
                            line = line[:-1]
                            print("\b \b", end="", flush=True)
                    elif char in ("\x00", "\xe0"):
                        if msvcrt.kbhit():
                            msvcrt.getwch()
                    elif char.isprintable():
                        line += char
                        print(char, end="", flush=True)
                thread.join()
                if "error" in outcome:
                    raise outcome["error"]
            prompt = args.prompt_file.read_text(encoding="utf-8-sig") if args.prompt_file else args.prompt
            if args.resume:
                original = tasks.resume(args.resume)
                run("Resume the saved task. Original request: " + original + ("\nAdditional instruction: " + prompt if prompt else ""), resume=True)
                return 0
            if prompt is not None:
                run(prompt)
                return 0
            ui.write("Commands: /menu, /history, /review, /models, /workspace PATH, /help")
            while True:
                try:
                    prompt = ui.prompt(config.model, workspace.root)
                except EOFError:
                    return 0
                if not prompt:
                    continue
                if prompt in {"/exit", "/quit"}:
                    return 0
                if prompt == "/help":
                    print("Enter a task prompt. /tools = available tools. /reset = clear conversation. "
                          "/history = saved tasks. /resume [number|id] = continue saved work. "
                          "/rerun [number|id] = run the original request again as a new task. "
                          "/workspace PATH = restart this session in another folder. "
                          "/models = list locally installed Ollama models. "
                          "/model NUMBER|NAME = switch to an installed model. "
                          "/review [number|id] = show files and commands from a task. "
                          "/artifacts [number|id] = show task artifacts. /ui compact|detailed = change display. "
                          "/settings = show active model limits. /clear logs|tasks|all = remove old records. "
                          "/context TOKENS and /timeout SECONDS change this session's model limits. "
                          "/trace on|off = toggle full terminal tracing. "
                          "/exit = quit. For multiline prompts use --prompt-file. "
                          "Conversation is kept within this session. Task behavior comes from your prompts/instructions.")
                    continue
                if prompt == "/menu":
                    ui.write("""Available actions
  Work       Type any request
  Progress   /status or /plan
  Results    /review, /artifacts
  History    /history, /history failed, /history search WORDS
  Continue   /resume NUMBER, /rerun NUMBER
  Models     /models, /model NUMBER
  Workspace  /workspace PATH
  Display    /ui compact, /ui detailed
  Setup      /setup
  Exit       /exit""")
                    continue
                if prompt == "/setup":
                    setup_wizard(client, config)
                    raise ReloadSession()
                if prompt in {"/ui compact", "/ui detailed"}:
                    ui.mode = prompt.split()[1]
                    ui.write(f"Interface mode changed to {ui.mode} for this session.")
                    continue
                if prompt == "/logs":
                    ui.write(str(log.path))
                    continue
                if prompt in {"/plan", "/status"}:
                    print(json.dumps(tasks.status(), indent=2, ensure_ascii=False))
                    continue
                if prompt == "/settings":
                    print(json.dumps({"model": config.model, "workspace": str(workspace.root),
                                      "ui_mode": ui.mode,
                                      "context_tokens": config.options.get("num_ctx"),
                                      "max_output_tokens": config.options.get("num_predict"),
                                      "model_timeout_seconds": config.timeout_seconds,
                                      "command_timeout_seconds": config.command_timeout_seconds,
                                      "max_steps": config.max_steps, "file_access": config.file_access},
                                     indent=2, ensure_ascii=False))
                    continue
                if prompt.startswith("/context "):
                    try:
                        value = int(prompt.split(maxsplit=1)[1])
                        if value < 4096:
                            raise ValueError
                    except ValueError:
                        print("Usage: /context TOKENS (minimum 4096), for example /context 32768")
                        continue
                    config.options["num_ctx"] = value
                    print(f"Context set to {value} tokens for this session. Larger values use more RAM/VRAM.")
                    continue
                if prompt.startswith("/timeout "):
                    try:
                        value = int(prompt.split(maxsplit=1)[1])
                        if value < 30:
                            raise ValueError
                    except ValueError:
                        print("Usage: /timeout SECONDS (minimum 30), for example /timeout 1800")
                        continue
                    config.timeout_seconds = value
                    client.timeout = value
                    print(f"Ollama response timeout set to {value} seconds for this session.")
                    continue
                if prompt.startswith("/workspace "):
                    target = Path(prompt.split(maxsplit=1)[1].strip().strip('"')).resolve()
                    if not target.is_dir():
                        print(f"Error: workspace directory does not exist: {target}", file=sys.stderr)
                        continue
                    print(f"Switching workspace to {target}...", flush=True)
                    raise WorkspaceSwitch(target)
                if prompt == "/models":
                    installed = client.models()
                    if not installed:
                        print("No Ollama models are installed. Use: ollama pull MODEL")
                    else:
                        print("Installed Ollama models:")
                        for number, name in enumerate(installed, 1):
                            marker = " * current" if name == config.model or name == config.model + ":latest" else ""
                            print(f"  {number}. {name}{marker}")
                        print("Switch with /model NUMBER or /model NAME")
                    continue
                if prompt == "/model" or prompt.startswith("/model "):
                    selector = prompt.split(maxsplit=1)[1].strip() if " " in prompt else ""
                    installed = client.models()
                    if not selector:
                        print("Usage: /model NUMBER or /model NAME. Run /models to see installed models.")
                        continue
                    if selector.isdigit():
                        number = int(selector)
                        if number < 1 or number > len(installed):
                            print(f"Model number must be between 1 and {len(installed)}. Run /models first.")
                            continue
                        selected = installed[number - 1]
                    else:
                        matches = [name for name in installed if name == selector or name == selector + ":latest"]
                        if not matches:
                            print(f"Model is not installed: {selector}. Run /models to see available models.")
                            continue
                        selected = matches[0]
                    if selected == config.model or selected == config.model + ":latest":
                        print(f"Already using {config.model}.")
                        continue
                    print(f"Switching model to {selected}...", flush=True)
                    raise ModelSwitch(selected)
                if prompt.startswith("/clear "):
                    scope = prompt.split(maxsplit=1)[1].strip().lower()
                    if scope not in {"logs", "tasks", "all"}:
                        print("Use /clear logs, /clear tasks, or /clear all.")
                        continue
                    removed = clear_saved_data(directory, scope, log.path,
                                               tasks.folder if tasks.folder else None)
                    print(f"Removed {removed['logs']} old logs and {removed['tasks']} old tasks. "
                          "The active session/task was kept.")
                    continue
                if prompt == "/history" or prompt.startswith("/history "):
                    entries = tasks.history()
                    query = prompt.split(maxsplit=1)[1].strip() if " " in prompt else ""
                    parts = query.split(maxsplit=1)
                    action = parts[0].lower() if parts else ""
                    value = parts[1].strip() if len(parts) > 1 else ""
                    if action in {"show", "artifacts"} or query.isdigit():
                        selector = value if action in {"show", "artifacts"} else query
                        if action == "artifacts":
                            try:
                                ui.show_json(tasks.review(selector or "latest"))
                            except ToolError as exc:
                                ui.write(f"Error: {exc}")
                            continue
                        if not selector.isdigit():
                            try:
                                ui.show_json(tasks.review(selector))
                            except ToolError as exc:
                                ui.write(f"Error: {exc}")
                            continue
                        number = int(selector)
                        if number < 1 or number > len(entries):
                            print(f"History number must be between 1 and {len(entries)}.")
                            continue
                        entry = entries[number - 1]
                        detail = {
                            "number": entry["number"], "task_id": entry["task_id"],
                            "status": entry["status"], "remaining_count": entry["remaining_count"],
                            "updated": datetime.fromtimestamp(entry["updated"]).astimezone().isoformat(timespec="seconds"),
                            "model": entry.get("model"), "artifact_count": entry.get("artifact_count", 0),
                            "request": entry["request"],
                            "plan_file": str(tasks.directory / entry["task_id"] / "plan.md"),
                        }
                        ui.show_json(detail)
                        continue
                    if action == "delete":
                        if not value:
                            ui.write("Usage: /history delete NUMBER|ID")
                            continue
                        try:
                            deleted = tasks.delete_history(value)
                            ui.write(f"Deleted saved task {deleted}. This cannot be resumed.")
                        except ToolError as exc:
                            ui.write(f"Error: {exc}")
                        continue
                    if action in {"complete", "completed", "blocked", "active", "planning"}:
                        wanted = "complete" if action == "completed" else action
                        entries = [entry for entry in entries if entry["status"] == wanted]
                    elif action == "failed":
                        entries = [entry for entry in entries if entry["status"] in {"blocked", "incomplete"}]
                    elif action == "search":
                        words = value.casefold().split()
                        entries = [entry for entry in entries
                                   if all(word in entry["request"].casefold() for word in words)]
                    elif query:
                        words = query.casefold().split()
                        entries = [entry for entry in entries
                                   if all(word in entry["request"].casefold() for word in words)]
                    ui.history(entries)
                    continue
                if prompt == "/review" or prompt.startswith("/review ") or prompt == "/artifacts" or prompt.startswith("/artifacts "):
                    selector = prompt.split(maxsplit=1)[1].strip() if " " in prompt else None
                    try:
                        ui.show_json(tasks.review(selector))
                    except ToolError as exc:
                        ui.write(f"Error: {exc}")
                    continue
                if prompt == "/resume" or prompt.startswith("/resume "):
                    try:
                        original = tasks.resume(prompt.split(maxsplit=1)[1] if " " in prompt else "latest")
                        agent.reset()
                        interactive_run("Resume this saved task without repeating completed steps: " + original, resume=True)
                    except (AgentError, OllamaError, ToolError, OSError, ValueError) as exc:
                        print(f"Error: {exc}", file=sys.stderr)
                    continue
                if prompt == "/rerun" or prompt.startswith("/rerun "):
                    try:
                        selector = prompt.split(maxsplit=1)[1] if " " in prompt else "latest"
                        original = tasks.historical_request(selector)
                        agent.reset()
                        interactive_run(original)
                    except (AgentError, OllamaError, ToolError, OSError, ValueError) as exc:
                        print(f"Error: {exc}", file=sys.stderr)
                    continue
                if prompt in {"/trace on", "/trace off"}:
                    config.trace = prompt == "/trace on"
                    print(f"Terminal trace {'enabled' if config.trace else 'disabled'}.")
                    continue
                if prompt == "/reset":
                    agent.reset()
                    print("Conversation cleared. Existing files are unchanged.")
                    continue
                if prompt == "/tools":
                    print("\n".join(f"{name}: {tool.description}" for name, tool in registry.tools.items()))
                    continue
                try:
                    # Each ordinary prompt is a new persisted task, so it must
                    # not inherit a previous task's large tool transcript.
                    agent.reset()
                    interactive_run(prompt)
                except (AgentError, OllamaError, ToolError, OSError, ValueError) as exc:
                    print(f"Error: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nStopped. Completed tool actions remain in effect. Inspect the run log before retrying commands.", file=sys.stderr)
        return 130
    except (AgentError, OllamaError, ToolError, OSError, ValueError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def main(argv=None):
    current = list(sys.argv[1:] if argv is None else argv)
    while True:
        try:
            return _main(current)
        except WorkspaceSwitch as switch:
            current = workspace_argv(current, switch.path)
        except ModelSwitch as switch:
            current = model_argv(current, switch.model)
        except ReloadSession:
            current = reload_argv(current)
