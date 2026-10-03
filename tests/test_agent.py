import json
import unittest

from local_agent.agent import Agent, AgentError, compact_tool_result
from local_agent.config import Config
from local_agent.ollama import OllamaError
from local_agent.registry import Registry, string


class Log:
    def write(self, *args, **kwargs):
        pass


class FakeClient:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.modes = []
        self.requests = []
        self.schemas = []

    def chat(self, messages, schemas, config, mode):
        self.modes.append(mode)
        self.requests.append(messages.copy())
        self.schemas.append(schemas)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply if "message" in reply else {"message": reply}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.executed = []
        self.registry = Registry()
        self.registry.add("save", "save", lambda text: self.executed.append(text),
                          {"text": string("text")}, ["text"])

    def agent(self, replies, **options):
        self.client = FakeClient(replies)
        return Agent(self.client, self.registry, Config(**options), "Test", Log(), emit=lambda x: None)

    def test_fenced_calls_and_repair_execute_once(self):
        agent = self.agent([
            {"content": '{"name":"save","arguments":{"text":}}'},
            {"content": "'''json\n{\"name\":\"save\",\"arguments\":{\"text\":\"hello\"}}\n'''"},
            {"content": "Saved."}])
        self.assertEqual(agent.run("Save hello"), "Saved.")
        self.assertEqual(self.executed, ["hello"])

    def test_all_calls_validated_before_any_execute(self):
        agent = self.agent([{ "tool_calls": [
            {"function": {"name": "save", "arguments": {"text": "should not run"}}},
            {"function": {"name": "unknown", "arguments": {}}}]}, {"content": "No changes."}])
        agent.run("test")
        self.assertEqual(self.executed, [])

    def test_api_tool_failure_falls_back_to_text(self):
        agent = self.agent([OllamaError("model does not support tools"),
                            {"content": '{"name":"save","arguments":{"text":"ok"}}'},
                            {"content": "done"}])
        self.assertEqual(agent.run("test"), "done")
        self.assertEqual(self.client.modes, ["native", "json", "json"])

    def test_repair_limit(self):
        agent = self.agent([{"content": ""}] * 3, max_repairs=1)
        with self.assertRaises(AgentError):
            agent.run("test")
        self.assertEqual(self.executed, [])

    def test_server_parse_failure_is_repaired_in_native_mode(self):
        error = OllamaError('Ollama HTTP 500: {"error":"error parsing tool call: invalid character colon after array element"}')
        agent = self.agent([error, error,
                            {"tool_calls": [{"function": {"name": "save", "arguments": {"text": "ok"}}}]},
                            {"content": "Saved."}])
        self.assertEqual(agent.run("save"), "Saved.")
        self.assertEqual(self.client.modes, ["native", "native", "native", "native"])
        self.assertEqual(self.executed, ["ok"])

    def test_server_parse_retry_is_bounded_in_json_mode(self):
        error = OllamaError("error parsing tool call: invalid character")
        agent = self.agent([error] * 3, tool_mode="json", max_repairs=1)
        with self.assertRaisesRegex(AgentError, "Server tool-call repair limit"):
            agent.run("save")
        self.assertEqual(len(self.client.requests), 2)
        self.assertEqual(self.executed, [])

    def test_unrelated_server_error_not_retried(self):
        agent = self.agent([OllamaError("Ollama HTTP 500: out of memory")])
        with self.assertRaises(OllamaError):
            agent.run("save")
        self.assertEqual(len(self.client.requests), 1)

    def test_unsupported_thinking_is_disabled_and_retried(self):
        agent = self.agent([OllamaError('HTTP 400: "model" does not support thinking'),
                            {"content": "done"}], think="low")
        self.assertEqual(agent.run("test"), "done")
        self.assertIsNone(agent.config.think)
        self.assertEqual(len(self.client.requests), 2)

    def test_compact_read_result_preserves_evidence_and_paginates_transport(self):
        result = {"ok": True, "evidence_id": "E000001", "result": {
            "path": "large.txt", "offset": 0, "content": "x" * 50, "truncated": False}}
        compact = json.loads(compact_tool_result("read_file", result, content_limit=10))
        self.assertEqual(compact["evidence_id"], "E000001")
        self.assertEqual(len(compact["result"]["content"]), 10)
        self.assertTrue(compact["result"]["transport_truncated"])
        self.assertEqual(compact["result"]["next_offset"], 10)

    def test_transient_transport_failure_retries_once(self):
        agent = self.agent([OllamaError("Remote end closed connection without response"),
                            {"content": "done"}])
        self.assertEqual(agent.run("test"), "done")
        self.assertEqual(len(self.client.requests), 2)

    def test_token_telemetry_tracks_prompt_generation_and_cache(self):
        agent = self.agent([{"message": {"content": "done"}, "prompt_eval_count": 120,
                             "prompt_eval_cached_count": 80, "eval_count": 12}])
        agent.run("test")
        usage = agent.token_usage()
        self.assertEqual(usage["prompt_tokens"], 120)
        self.assertEqual(usage["cached_prompt_tokens"], 80)
        self.assertEqual(usage["generated_tokens"], 12)
        self.assertGreater(usage["schema_characters"], 0)

    def test_explicit_native_mode_repairs_without_switching_modes(self):
        agent = self.agent([OllamaError("error parsing tool call: invalid character"),
                            {"tool_calls": [{"function": {"name": "save", "arguments": {"text": "ok"}}}]},
                            {"content": "done"}], tool_mode="native")
        self.assertEqual(agent.run("save"), "done")
        self.assertEqual(self.client.modes, ["native"] * 3)
        self.assertEqual(self.executed, ["ok"])

    def test_truncated_reply_never_executes(self):
        agent = self.agent([{"message": {"tool_calls": [{"function": {"name": "save", "arguments": {"text": "x"}}}]},
                             "done_reason": "length"}])
        with self.assertRaises(AgentError):
            agent.run("test")
        self.assertEqual(self.executed, [])

    def test_conversation_keeps_followup_context_and_reset_clears_it(self):
        agent = self.agent([{"content": "The file is notes.txt."}, {"content": "It contains notes."},
                            {"content": "Fresh answer."}])
        agent.run("Identify the file")
        agent.run("What does it contain?")
        self.assertTrue(any(m.get("content") == "The file is notes.txt." for m in self.client.requests[1]))
        agent.reset()
        agent.run("Start another task")
        self.assertFalse(any(m.get("content") == "The file is notes.txt." for m in self.client.requests[2]))
