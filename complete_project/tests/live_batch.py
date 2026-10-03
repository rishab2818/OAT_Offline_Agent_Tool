"""Opt-in runtime-planning check: setup once, two items, finish once.

Only a newly allocated temporary workspace is changed. The test deliberately
does not supply a plan: the real model must construct and execute it.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def verify(root):
    assert (root / "setup.txt").read_text().strip() == "ready"
    assert (root / "finished.txt").read_text().strip() == "complete"
    for name, text in (("alpha.txt", "alpha: 17"), ("beta.txt", "beta: 29")):
        assert (root / "output" / (name + ".md")).read_text().strip() == text
    states = [json.loads(p.read_text()) for p in (root / ".local-agent/tasks").glob("*/plan.json")]
    assert states and states[-1]["status"] == "complete"
    evidence = [json.loads(p.read_text()) for p in (root / ".local-agent/tasks").glob("*/evidence/*.json")]
    writes = [e for e in evidence if e["tool"] == "write_file" and e.get("result", {}).get("ok")]
    for filename in ("setup.txt", "finished.txt"):
        assert sum(Path(e["arguments"]["path"]).name == filename for e in writes) == 1
    assert any(s["kind"] == "foreach" and s.get("item_count") == 2 for s in states[-1]["steps"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-oss:20b")
    parser.add_argument("--tool-mode", default="json", choices=["auto", "native", "json"])
    parser.add_argument("--verify-workspace", type=Path)
    args = parser.parse_args()
    if args.verify_workspace:
        verify(args.verify_workspace)
        print("RETAINED BATCH VERIFIED:", args.verify_workspace)
        return
    project = Path(__file__).resolve().parents[1]
    root = Path(tempfile.mkdtemp(prefix="local-agent-batch-"))
    (root / "items.txt").write_text("alpha.txt\nbeta.txt\n", encoding="utf-8")
    (root / "alpha.txt").write_text("alpha: 17", encoding="utf-8")
    (root / "beta.txt").write_text("beta: 29", encoding="utf-8")
    config = json.loads((project / "config.json").read_text())
    config.update(workspace=str(root), model=args.model, tool_mode=args.tool_mode,
                  trace=False, system_prompt=str(project / "prompts/assistant_system.md"),
                  max_steps=45)
    (root / "test-config.json").write_text(json.dumps(config), encoding="utf-8")
    print("Batch workspace (retained):", root, flush=True)
    prompt = ("Create setup.txt containing ready once. Read items.txt to discover the items. "
              "For each listed filename, read it and write its exact content to output/<filename>.md. "
              "When all items are done, create finished.txt containing complete once. "
              "Do not change the input files. Use a runtime foreach plan for the discovered items. "
              "Do not put future write content in tool checks; match paths. Use one action per item.")
    result = subprocess.run([sys.executable, "-u", str(project / "main.py"), "--config", str(root / "test-config.json"),
                             "--prompt", prompt], check=False)
    if result.returncode:
        raise SystemExit(result.returncode)
    verify(root)
    print("LIVE BATCH PASSED:", root, flush=True)


if __name__ == "__main__":
    main()
