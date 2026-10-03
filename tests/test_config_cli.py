import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.cli import clear_saved_data, load_instructions, model_argv, workspace_argv
from local_agent.config import Config
from local_agent.workspace import Workspace
from local_agent.workflow_contract import workflow_contract


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_task_instructions_are_opt_in_and_language_independent(self):
        (self.root / "SKILL.md").write_text("Generate revision history for Python.", encoding="utf-8")
        ws = Workspace(self.root)
        self.assertNotIn("revision history", load_instructions(Config(), ws))
        config = Config(instruction_files=["SKILL.md"])
        self.assertIn("revision history", load_instructions(config, ws))
        (self.root / "SKILL.md").write_text("Summarize C functions.", encoding="utf-8")
        self.assertIn("Summarize C", load_instructions(config, ws))

    def test_optional_agent_file(self):
        (self.root / "agent.md").write_text("Use concise answers.", encoding="utf-8")
        self.assertIn("Use concise answers", load_instructions(Config(), Workspace(self.root)))

    def test_paths_resolve_relative_to_config(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"workspace": ".", "system_prompt": "prompt.md"}), encoding="utf-8")
        config = Config.load(path)
        self.assertEqual(config.workspace, str(self.root))
        self.assertEqual(config.system_prompt, str(self.root / "prompt.md"))

    def test_default_workspace_is_launch_directory(self):
        config_path = self.root / "config.json"
        launch = self.root / "launched-here"
        launch.mkdir()
        for data in ({}, {"workspace": None}):
            config_path.write_text(json.dumps(data), encoding="utf-8")
            with patch("local_agent.config.Path.cwd", return_value=launch):
                self.assertEqual(Config.load(config_path).workspace, str(launch))

    def test_cli_workspace_override_wins(self):
        path = self.root / "config.json"
        path.write_text('{"workspace":".."}', encoding="utf-8")
        chosen = self.root / "chosen"
        self.assertEqual(Config.load(path, {"workspace": str(chosen)}).workspace, str(chosen))

    def test_markdown_workflow_compiles_once_and_repeat_requirements(self):
        text = """# Plan
## Workflow
### 1. Create Queue
Do it once.
### 2. Select File
Read first.
### 3. Write Output
Return to Step 2.
## Notes
Not a workflow step.
"""
        result = workflow_contract([("PLAN.md", text)])
        self.assertEqual([r["title"] for r in result], ["Create Queue", "Select File", "Write Output"])
        self.assertEqual([r["scope"] for r in result], ["once", "repeat", "repeat"])

    def test_workspace_switch_replaces_existing_cli_option(self):
        self.assertEqual(workspace_argv(["--trace", "--workspace", "old", "--model", "x"], "new"),
                         ["--trace", "--model", "x", "--workspace", "new"])
        self.assertEqual(workspace_argv(["--workspace=old"], "new"), ["--workspace", "new"])

    def test_model_switch_replaces_existing_cli_option(self):
        self.assertEqual(model_argv(["--trace", "--model", "old", "--workspace", "x"], "new:7b"),
                         ["--trace", "--workspace", "x", "--model", "new:7b"])
        self.assertEqual(model_argv(["--model=old"], "new:7b"), ["--model", "new:7b"])
        self.assertEqual(model_argv(["--model", "old"], "new:7b", resume=True),
                         ["--model", "new:7b", "--resume", "latest"])

    def test_clear_saved_data_keeps_active_records(self):
        private = self.root / ".local-agent"
        tasks = private / "tasks"
        tasks.mkdir(parents=True)
        active_log = private / "run-active.jsonl"
        old_log = private / "run-old.jsonl"
        active_log.write_text("active")
        old_log.write_text("old")
        active_task = tasks / "active"
        old_task = tasks / "old"
        active_task.mkdir()
        old_task.mkdir()
        result = clear_saved_data(private, "all", active_log, active_task)
        self.assertEqual(result, {"logs": 1, "tasks": 1})
        self.assertTrue(active_log.exists())
        self.assertTrue(active_task.exists())
        self.assertFalse(old_log.exists())
        self.assertFalse(old_task.exists())
