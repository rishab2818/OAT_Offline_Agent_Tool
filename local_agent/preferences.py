"""Persistent per-user setup preferences, separate from project configuration."""
import json
import os
from pathlib import Path


def preferences_path():
    override = os.environ.get("OAT_CONFIG_HOME")
    if override:
        return Path(override).expanduser().resolve() / "settings.json"
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "OAT" / "settings.json"
    return Path.home() / ".config" / "oat" / "settings.json"


def load_preferences(path=None):
    path = Path(path or preferences_path())
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save_preferences(values, path=None):
    path = Path(path or preferences_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(values, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _ask(input_fn, output, prompt, default):
    answer = input_fn(f"{prompt} [{default}]: ").strip()
    return answer or str(default)


def setup_wizard(client, config, input_fn=input, output=print, path=None):
    """Run a small, restart-safe first-run wizard and persist its choices."""
    output("\nOAT first-run setup")
    output("This configures defaults only; command-line options can still override them.\n")
    models = client.models()
    if models:
        output("Installed Ollama models:")
        for number, model in enumerate(models, 1):
            marker = " (current)" if model == config.model or model == config.model + ":latest" else ""
            output(f"  {number}. {model}{marker}")
        while True:
            selected = _ask(input_fn, output, "Default model number or exact name", config.model)
            if selected.isdigit() and 1 <= int(selected) <= len(models):
                model = models[int(selected) - 1]
                break
            matches = [name for name in models if name == selected or name == selected + ":latest"]
            if matches:
                model = matches[0]
                break
            output("Choose one of the installed models shown above.")
    else:
        output("No installed Ollama models were found. Keeping the configured model.")
        model = config.model
    while True:
        workspace_text = _ask(input_fn, output, "Default workspace", config.workspace)
        workspace = Path(workspace_text.strip('"')).expanduser().resolve()
        if workspace.is_dir():
            break
        output("That directory does not exist. Enter an existing folder.")
    while True:
        try:
            context = int(_ask(input_fn, output, "Context tokens", config.options.get("num_ctx", 32768)))
            if context < 4096:
                raise ValueError
            break
        except ValueError:
            output("Context must be an integer of at least 4096.")
    while True:
        try:
            timeout = int(_ask(input_fn, output, "Model timeout in seconds", config.timeout_seconds))
            if timeout < 30:
                raise ValueError
            break
        except ValueError:
            output("Timeout must be an integer of at least 30 seconds.")
    mode = _ask(input_fn, output, "Interface mode (compact or detailed)", "compact").lower()
    if mode not in {"compact", "detailed"}:
        mode = "compact"
    values = {"model": model, "workspace": str(workspace), "context_tokens": context,
              "timeout_seconds": timeout, "ui_mode": mode, "setup_complete": True}
    saved = save_preferences(values, path)
    output(f"\nSetup saved to {saved}")
    return values
