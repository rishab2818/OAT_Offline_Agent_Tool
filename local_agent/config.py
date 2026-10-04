"""Typed runtime configuration, independent of task domain."""
import json
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class Config:
    model: str = "gpt-oss:20b"
    base_url: str = "http://127.0.0.1:11434"
    workspace: str | None = None
    trace: bool = False
    system_prompt: str = ""
    instruction_files: list[str] = field(default_factory=list)
    agent_file: str | None = None
    max_stalled_steps: int = 16
    file_access: str = "any"
    enabled_tools: list[str] = field(default_factory=lambda: [
        "list_files", "find_files", "glob_files", "read_file", "search_text", "write_file", "edit_file", "run_command"])
    timeout_seconds: int = 1800
    max_steps: int = 80
    max_repairs: int = 3
    max_context_chars: int = 100000
    max_file_chars: int = 40000
    read_chunk_chars: int = 12000
    command_timeout_seconds: int = 120
    bash_executable: str | None = None
    tool_mode: str = "auto"
    think: str | bool | None = None
    options: dict = field(default_factory=lambda: {
        "temperature": 0, "num_ctx": 32768, "num_predict": 4096})

    @classmethod
    def load(cls, path, overrides=None):
        path = Path(path).resolve()
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("Configuration must be a JSON object")
        unknown = set(raw) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown configuration keys: {sorted(unknown)}")
        raw.update({k: v for k, v in (overrides or {}).items() if v is not None})
        config = cls(**raw)
        # No configured workspace means the directory from which the user launched
        # the CLI. Explicit config paths remain relative to the config file.
        config.workspace = str(Path.cwd() if config.workspace is None else
                               (path.parent / config.workspace).resolve())
        if config.system_prompt:
            config.system_prompt = str((path.parent / config.system_prompt).resolve())
        if config.file_access not in {"any", "workspace"}:
            raise ValueError("file_access must be any or workspace")
        if type(config.trace) is not bool:
            raise ValueError("trace must be true or false")
        if config.tool_mode not in {"auto", "native", "json"}:
            raise ValueError("tool_mode must be auto, native, or json")
        for key in ("timeout_seconds", "max_steps", "max_repairs", "max_context_chars", "max_file_chars", "read_chunk_chars", "command_timeout_seconds", "max_stalled_steps"):
            if type(getattr(config, key)) is not int or getattr(config, key) < 1:
                raise ValueError(f"{key} must be a positive integer")
        if not isinstance(config.instruction_files, list) or not all(
                isinstance(p, str) for p in config.instruction_files):
            raise ValueError("instruction_files must be a list of paths")
        if not isinstance(config.options, dict):
            raise ValueError("options must be an object")
        for key in ("num_ctx", "num_predict"):
            if key in config.options and (type(config.options[key]) is not int or config.options[key] < 1):
                raise ValueError(f"options.{key} must be a positive integer")
        if not isinstance(config.enabled_tools, list) or not all(isinstance(n, str) for n in config.enabled_tools):
            raise ValueError("enabled_tools must be a list of tool names")
        return config
