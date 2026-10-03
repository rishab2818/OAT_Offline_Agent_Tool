import json
import unittest

from local_agent.protocol import ProtocolError, parse_message
from local_agent.registry import Registry, string
from local_agent.workspace import ToolError


class ProtocolTests(unittest.TestCase):
    def test_native_and_fenced_formats(self):
        fn = {"name": "read_file", "arguments": {"path": "notes.txt"}}
        forms = [
            {"tool_calls": [{"function": fn}]},
            {"content": json.dumps(fn)},
            {"content": json.dumps(json.dumps(fn))},
            {"content": "```json\n" + json.dumps({"tool_calls": [{"function": fn}]}) + "\n```"},
            {"content": "'''json\n" + json.dumps(fn) + "\n'''"},
            {"tool_calls": [{"function": {"name": "read_file", "arguments":
                                         "```json\n{\"path\":\"notes.txt\"}\n```"}}]},
            {"tool_calls": json.dumps([{"function": fn}])},
            {"tool_calls": [{"function": {"name": "read_file", "arguments":
                                         json.dumps(json.dumps(fn["arguments"]))}}]},
        ]
        for form in forms:
            with self.subTest(form=form):
                calls, _ = parse_message(form)
                self.assertEqual((calls[0].name, calls[0].arguments), ("read_file", {"path": "notes.txt"}))

    def test_no_extraction_from_prose_or_thinking(self):
        with self.assertRaises(ProtocolError):
            parse_message({"content": 'Here is an example: {"tool_calls": [{"function": {"name":"delete"}}]}'})
        calls, _ = parse_message({"thinking": '{"name":"run_command"}', "content": "I have an answer."})
        self.assertEqual(calls, [])

    def test_observed_dispatch_wrapper_and_known_namespaces(self):
        arguments = {"evidence_id": "E000002", "field": "content", "format": "lines", "step_id": "process_file"}
        wrapped = {"tool_calls": [{"id": "call_x", "function": {"index": 0, "name": "tool.run_command",
                   "arguments": {"name": "expand_task", "arguments": arguments}}}]}
        calls, _ = parse_message(wrapped)
        self.assertEqual(calls[0].name, "expand_task")
        self.assertEqual(calls[0].arguments, arguments)
        for name in ("tool.expand_task", "functions.expand_task"):
            self.assertEqual(parse_message({"tool_calls": [{"function": {"name": name, "arguments": arguments}}]})[0][0].name,
                             "expand_task")

    def test_wrapper_never_bypasses_registry_or_uses_shell_payload(self):
        registry = Registry()
        registry.add("read_file", "read", lambda path: path, {"path": string("path")}, ["path"])
        for arguments in ({"name": "disabled", "arguments": {}},
                          {"name": "read_file", "arguments": {"path": "x"}, "command": "danger"}):
            parsed = parse_message({"tool_calls": [{"function": {"name": "tool.run_command", "arguments": arguments}}]})[0][0]
            with self.assertRaises(ToolError):
                registry.validate(parsed)
        with self.assertRaises(ProtocolError):
            parse_message({"tool_calls": [{"function": {"name": "arbitrary.read_file", "arguments": {}}}]})

    def test_regular_json_answers_are_not_tool_calls(self):
        for content in ('{"name":"sensor","value":3}', '[{"name":"alpha"},{"name":"beta"}]',
                        '[1,2,3]', '```json\n{"name":"sensor"}\n```'):
            with self.subTest(content=content):
                self.assertEqual(parse_message({"content": content})[0], [])

    def test_invalid_json_never_guessed(self):
        for content in ('{"name":"read_file","arguments":{"path":"x",}}',
                        '{"name":"read_file","arguments": ["x"]}',
                        '```json\n{"name": "read_file"',
                        '{"name":"read_file","arguments":{},"unexpected":true}'):
            with self.subTest(content=content), self.assertRaises(ProtocolError):
                parse_message({"content": content})

    def test_schema_rejects_unknown_names_keys_and_wrong_types(self):
        registry = Registry()
        registry.add("read_file", "read", lambda path: path, {"path": string("path")}, ["path"])
        for fn in ({"name": "unknown", "arguments": {}},
                   {"name": "read_file", "arguments": {}},
                   {"name": "read_file", "arguments": {"path": 123}},
                   {"name": "read_file", "arguments": {"path": "x", "other": 1}}):
            with self.subTest(fn=fn), self.assertRaises(ToolError):
                registry.validate(parse_message({"content": json.dumps(fn)})[0][0])
