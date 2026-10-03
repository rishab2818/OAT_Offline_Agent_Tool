"""Small dependency-free terminal UI for readable local-agent sessions."""
import json
import sys
from pathlib import Path


class TerminalUI:
    def __init__(self, mode="compact", stream=None):
        self.mode = mode if mode in {"compact", "detailed"} else "compact"
        self.stream = stream or sys.stdout

    def write(self, text="", end="\n"):
        print(text, end=end, file=self.stream, flush=True)

    def banner(self, version, config, log_path):
        name = Path(config.workspace).name or config.workspace
        self.write(f"\nOAT {version}  |  {config.model}  |  {name}")
        self.write(f"Workspace: {config.workspace}")
        self.write(f"Context: {config.options.get('num_ctx', 'default')}  Timeout: {config.timeout_seconds}s  UI: {self.mode}")
        if self.mode == "detailed":
            self.write(f"Log: {log_path}")

    def prompt(self, model, workspace):
        return input(f"\n[{model} | {Path(workspace).name}] You> ").strip()

    def emit(self, value):
        value = str(value)
        if self.mode == "detailed":
            self.write(value)
            return
        if value.startswith("[model]"):
            return
        mappings = {
            "[tool] ": "  -> ", "[saved] ": "  + saved ", "[tool error] ": "  ! ",
            "[protocol] ": "  i ", "[task] ": "  i ", "[plan] ": "  task record: ",
        }
        for prefix, replacement in mappings.items():
            if value.startswith(prefix):
                self.write(replacement + value[len(prefix):])
                return
        self.write(value)

    def task_start(self, prompt):
        summary = " ".join(prompt.split())
        self.write("\nWorking: " + (summary[:117] + "..." if len(summary) > 120 else summary))

    def task_complete(self, answer, review=None):
        self.write("\nDone")
        if review:
            files = review.get("files", [])
            commands = review.get("commands", [])
            if files:
                self.write(f"Files touched: {len(files)}")
                for item in files[:8]:
                    self.write(f"  {item['operation']}: {item['path']}")
            if commands:
                self.write(f"Commands run: {len(commands)}")
                for item in commands[:5]:
                    self.write(f"  exit {item.get('exit_code')}: {item['command']}")
        self.write("\n" + answer)

    def show_json(self, value):
        self.write(json.dumps(value, indent=2, ensure_ascii=False))

    def history(self, entries):
        if not entries:
            self.write("No matching saved tasks in this workspace.")
            return
        for entry in entries:
            request = " ".join(entry.get("request", "").split())
            if len(request) > 120:
                request = request[:117] + "..."
            model = entry.get("model") or "unknown model"
            self.write(f"{entry['number']:>3}. [{entry['status']}] {request}")
            self.write(f"     {model} | artifacts={entry.get('artifact_count', 0)} | {entry['task_id']}")
