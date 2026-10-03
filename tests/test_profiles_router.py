import json
import tempfile
import unittest
from pathlib import Path

from local_agent.instruction_router import InstructionRouter
from local_agent.ollama import OllamaError
from local_agent.profiles import ModelProfiles


class ProfileClient:
    def __init__(self, reject_thinking=True):
        self.reject_thinking = reject_thinking
        self.calls = []

    def request(self, endpoint, payload):
        self.calls.append(payload)
        if payload.get("think") and self.reject_thinking:
            self.reject_thinking = False
            raise OllamaError('"model" does not support thinking')
        if payload.get("format") == "json":
            return {"message": {"content": '{"ok":true}'}, "eval_count": 2, "eval_duration": 1_000_000_000}
        return {"message": {"tool_calls": [{"function": {"name": "ping", "arguments": {}}}]},
                "eval_count": 4, "eval_duration": 1_000_000_000}


class ProfilesAndRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_profile_probes_and_persists_capabilities(self):
        profiles = ModelProfiles(self.root / "profiles.json")
        profile = profiles.probe(ProfileClient(), "coder:7b", 32768)
        self.assertFalse(profile["thinking_supported"])
        self.assertEqual(profile["native_tool_reliability"], 1.0)
        self.assertTrue(profile["json_supported"])
        self.assertEqual(profile["stable_context_tokens"], 8192)
        loaded = json.loads((self.root / "profiles.json").read_text())
        self.assertEqual(loaded["coder:7b"]["recommended_tool_mode"], "native")

    def test_existing_profile_is_reused_without_probe(self):
        profiles = ModelProfiles(self.root / "profiles.json")
        client = ProfileClient(False)
        first = profiles.probe(client, "coder:7b")
        count = len(client.calls)
        second = profiles.probe(client, "coder:7b")
        self.assertEqual(first, second)
        self.assertEqual(len(client.calls), count)

    def test_instruction_router_selects_current_workflow_section(self):
        plan = """# Plan
## Rules
- Never modify source.
## Workflow
### 1. Create Queue
Run queue.py.
### 2. Write Output
Write output/{item}.md.
"""
        router = InstructionRouter([("PLAN.md", plan)], self.root)
        routed = router.for_action({"title": "Write Output"})
        self.assertIn("Never modify source", routed)
        self.assertIn("Write output", routed)
        self.assertNotIn("Run queue.py", routed)
