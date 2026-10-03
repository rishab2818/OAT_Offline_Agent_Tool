import io
import tempfile
import unittest
from pathlib import Path

from local_agent.config import Config
from local_agent.preferences import load_preferences, save_preferences, setup_wizard
from local_agent.ui import TerminalUI


class FakeModels:
    def models(self):
        return ["small:latest", "coder:7b"]


class PreferencesAndUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_preferences_round_trip_and_corrupt_file_fallback(self):
        path = self.root / "settings.json"
        save_preferences({"model": "coder:7b", "setup_complete": True}, path)
        self.assertEqual(load_preferences(path)["model"], "coder:7b")
        path.write_text("not json", encoding="utf-8")
        self.assertEqual(load_preferences(path), {})

    def test_setup_wizard_validates_and_persists_choices(self):
        path = self.root / "settings.json"
        answers = iter(["2", str(self.root), "65536", "2400", "detailed"])
        output = []
        config = Config(model="small:latest", workspace=str(self.root))
        result = setup_wizard(FakeModels(), config, input_fn=lambda _: next(answers),
                              output=output.append, path=path)
        self.assertEqual(result["model"], "coder:7b")
        self.assertEqual(result["context_tokens"], 65536)
        self.assertEqual(result["ui_mode"], "detailed")
        self.assertTrue(load_preferences(path)["setup_complete"])

    def test_compact_ui_hides_model_steps_and_summarizes_artifacts(self):
        stream = io.StringIO()
        ui = TerminalUI("compact", stream)
        ui.emit("[model] step 2, mode=native")
        ui.emit("[tool] write_file")
        ui.task_complete("Finished.", {"files": [{"operation": "write_file", "path": "x.txt"}],
                                       "commands": [{"command": "python x.py", "exit_code": 0}]})
        text = stream.getvalue()
        self.assertNotIn("step 2", text)
        self.assertIn("write_file", text)
        self.assertIn("x.txt", text)
        self.assertIn("exit 0", text)
