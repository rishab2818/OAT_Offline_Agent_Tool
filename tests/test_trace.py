import unittest

from local_agent.agent import Agent
from local_agent.config import Config
from local_agent.registry import Registry, string
from local_agent.trace import display_trace
from test_agent import FakeClient, Log


class TraceTests(unittest.TestCase):
    def test_multiline_text_and_windows_paths_are_readable(self):
        output = []
        display_trace(output.append, "REQUEST", {"content": "first\nsecond", "path": "D:\\notes\\README.md"})
        text = "\n".join(output)
        self.assertIn("first\n", text)
        self.assertIn("D:\\notes\\README.md", text)
        self.assertNotIn("D:\\\\notes", text)

    def test_tool_error_is_shown_and_sent_back(self):
        registry = Registry()
        def missing(path):
            raise FileNotFoundError("Missing README.md")
        registry.add("read_file", "Read", missing, {"path": string("path")}, ["path"])
        client = FakeClient([{"tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "README.md"}}}]},
                             {"content": "Could not find it."}])
        output = []
        agent = Agent(client, registry, Config(trace=True), "Test", Log(), emit=output.append)
        agent.run("Read README.md")
        text = "\n".join(output)
        self.assertIn("TOOL CALL", text)
        self.assertIn("TOOL RESULT -> MODEL", text)
        self.assertIn("Missing README.md", text)
        self.assertIn("Missing README.md", client.requests[1][-1]["content"])

    def test_trace_off_suppresses_verbose_tool_blocks(self):
        registry = Registry()
        registry.add("ping", "Ping", lambda: "pong")
        client = FakeClient([{"tool_calls": [{"function": {"name": "ping", "arguments": {}}}]}, {"content": "done"}])
        output = []
        Agent(client, registry, Config(trace=False), "Test", Log(), emit=output.append).run("ping")
        self.assertNotIn("TOOL RESULT -> MODEL", "\n".join(output))
