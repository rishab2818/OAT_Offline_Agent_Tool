"""Opt-in real Ollama test. Uses and retains a temporary folder for inspection."""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def verify(root):
    assert (root / "result.txt").read_text(encoding="utf-8").strip() == "smoke passed."
    events = [json.loads(line) for file in (root / ".local-agent").glob("run-*.jsonl")
              for line in file.read_text(encoding="utf-8").splitlines()]
    calls = {e["name"] for e in events if e["event"] == "tool" and e["result"]["ok"]}
    assert {"read_file", "run_command", "write_file"} <= calls
    commands = [e["result"]["result"] for e in events
                if e["event"] == "tool" and e["name"] == "run_command" and e["result"]["ok"]]
    assert any(r["exit_code"] == 0 and "SMOKE_OK" in r["stdout"] for r in commands)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-oss:20b")
    parser.add_argument("--tool-mode", default="auto", choices=["auto", "native", "json"])
    parser.add_argument("--trace", action="store_true", help="Print full requests and replies")
    parser.add_argument("--verify-workspace", type=Path, help="Re-check a retained smoke workspace without calling the model")
    args = parser.parse_args()
    if args.verify_workspace:
        verify(args.verify_workspace)
        print(f"RETAINED SMOKE VERIFIED: {args.verify_workspace}")
        return
    project = Path(__file__).resolve().parents[1]
    root = Path(tempfile.mkdtemp(prefix="local-agent-smoke-"))
    (root / "notes.txt").write_text("Project Aurora has three sensors. The review is Friday.\n", encoding="utf-8")
    config = json.loads((project / "config.json").read_text())
    config.update(workspace=str(root), model=args.model, tool_mode=args.tool_mode, trace=args.trace,
                  system_prompt=str(project / "prompts/assistant_system.md"), max_steps=30,
                  options={"temperature": 0, "num_ctx": 16384, "num_predict": 4096})
    (root / "smoke-config.json").write_text(json.dumps(config), encoding="utf-8")
    print(f"Smoke workspace (retained): {root}", flush=True)
    prompt = ("Read notes.txt. Run this PowerShell command: Write-Output 'SMOKE_OK'. "
              'If the output contains SMOKE_OK, create result.txt containing exactly "smoke passed." (without quotes). '
              "Then summarize notes.txt in one sentence. Use tools; do not just describe actions.")
    result = subprocess.run([sys.executable, "-u", str(project / "main.py"), "--config", str(root / "smoke-config.json"),
                             "--prompt", prompt], check=False)
    if result.returncode:
        raise SystemExit(result.returncode)
    verify(root)
    print(f"LIVE SMOKE PASSED ({args.model}, {args.tool_mode}). Logs: {root / '.local-agent'}", flush=True)


if __name__ == "__main__":
    main()
