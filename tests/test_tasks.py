import json
import tempfile
import unittest
from pathlib import Path

from local_agent.agent import Agent, AgentError
from local_agent.config import Config
from local_agent.file_tools import register_file_tools
from local_agent.protocol import Call
from local_agent.registry import Registry
from local_agent.storage import private_directory
from local_agent.tasks import TaskManager
from local_agent.workspace import Workspace, ToolError
from test_agent import FakeClient, Log


def action(key, checks):
    return {"id": key, "title": key, "kind": "action", "checks": checks}


def tool(name, **arguments):
    return {"type": "tool", "name": name, "arguments": arguments}


def call(tool_name, **arguments):
    return {"tool_calls": [{"function": {"name": tool_name, "arguments": arguments}}]}


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Config(trace=False, workspace=str(self.root))
        self.ws = Workspace(self.root)
        self.directory = private_directory(self.ws)
        self.registry = Registry()
        register_file_tools(self.registry, self.ws, self.config)
        self.manager = TaskManager(self.ws, self.directory, self.config.enabled_tools)
        self.manager.register(self.registry)
        self.manager.begin("Test task")

    def execute(self, name, **arguments):
        request = Call(name, arguments)
        event, cached = self.manager.before_tool(request)
        return cached if cached is not None else self.manager.after_tool(event, self.registry.execute(request))

    def test_mutations_require_plan_but_readonly_discovery_can_precede_it(self):
        (self.root / "input.txt").write_text("data", encoding="utf-8")
        self.assertTrue(self.execute("read_file", path="input.txt")["ok"])
        with self.assertRaisesRegex(ToolError, "plan"):
            self.execute("write_file", path="out.txt", content="data")

    def test_mutation_cannot_run_under_unrelated_current_step(self):
        self.manager.plan_task([action("read", [tool("read_file", path="input.txt")]),
                                action("write", [tool("write_file", path="out.txt")])])
        with self.assertRaisesRegex(ToolError, "does not belong to current step"):
            self.execute("write_file", path="out.txt", content="too early")

    def test_read_cannot_skip_the_file_named_by_current_step(self):
        self.manager.plan_task([action("first", [tool("read_file", path="input.txt")]),
                                action("second", [tool("read_file", path="other.txt")])])
        with self.assertRaisesRegex(ToolError, "planned file first"):
            self.execute("read_file", path="other.txt")

    def test_satisfied_action_hides_execution_tools_until_completed(self):
        (self.root / "input.txt").write_text("test content", encoding="utf-8")
        self.manager.plan_task([action("read", [tool("read_file", path="input.txt")])])
        result = self.execute("read_file", path="input.txt")
        allowed = self.manager.allowed_tools()
        self.assertNotIn("read_file", allowed)
        self.assertIn("complete_task_step", allowed)
        self.manager.complete_task_step("read", [result["evidence_id"]], "Input contains test content")

    def test_incomplete_action_hides_completion_until_checks_are_satisfied(self):
        (self.root / "input.txt").write_text("content", encoding="utf-8")
        self.manager.plan_task([action("read", [tool("read_file", path="input.txt")])])
        allowed = self.manager.allowed_tools()
        self.assertIn("read_file", allowed)
        self.assertNotIn("complete_task_step", allowed)

    def test_plan_schema_describes_actions_groups_and_disallows_host_fields(self):
        schema = self.registry.tools["plan_task"].properties["steps"]
        action_schema, group_schema = schema["items"]["anyOf"]
        self.assertFalse(action_schema["additionalProperties"])
        self.assertNotIn("evidence", action_schema["properties"])
        self.assertIn("checks", action_schema["required"])
        self.assertEqual(group_schema["properties"]["kind"]["const"], "foreach")
        self.assertIn("items", group_schema["properties"]["steps"])

    def test_selected_workflow_contract_rejects_partial_and_flat_repeat_plan(self):
        requirements = [
            {"id": "workflow_1", "title": "Create Queue", "source": "PLAN.md", "scope": "once"},
            {"id": "workflow_2", "title": "Process File", "source": "PLAN.md", "scope": "repeat"},
        ]
        manager = TaskManager(self.ws, self.directory, self.config.enabled_tools, requirements)
        manager.begin("follow the selected workflow")
        with self.assertRaisesRegex(ToolError, "not covered"):
            manager.plan_task([{**action("queue", [{"type": "answer"}]), "covers": ["workflow_1"]}])
        with self.assertRaisesRegex(ToolError, "inside foreach"):
            manager.plan_task([
                {**action("queue", [{"type": "answer"}]), "covers": ["workflow_1"]},
                {**action("file", [{"type": "answer"}]), "covers": ["workflow_2"]},
            ])
        manager.plan_task([
            {**action("queue", [{"type": "answer"}]), "covers": ["workflow_1"]},
            {"id": "each", "title": "each", "kind": "foreach", "covers": ["workflow_2"],
             "steps": [action("work", [{"type": "answer"}])]},
        ])
        self.assertEqual(manager.state["status"], "active")

    def test_plan_normalizes_premature_document_content_constraints(self):
        self.manager.plan_task([action("write", [tool("write_file", path="out.md", content="<generated content>")])])
        check = self.manager.state["steps"][0]["checks"][0]
        self.assertEqual(check, tool("write_file", path="out.md"))

    def test_actual_write_gets_file_verification_even_when_plan_omits_it(self):
        self.manager.plan_task([action("write", [tool("write_file")])])
        result = self.execute("write_file", path="out.md", content="real document")
        self.manager.complete_task_step("write", [result["evidence_id"]], "saved")
        (self.root / "out.md").unlink()
        self.assertIn("missing", self.manager.finalize())

    def test_forged_failed_and_other_step_evidence_cannot_complete(self):
        self.manager.plan_task([action("first", [tool("read_file", path="missing.txt")]), action("second", [tool("read_file")])])
        with self.assertRaisesRegex(ToolError, "Unknown evidence"):
            self.manager.complete_task_step("first", ["E999999"], "done")
        result = self.execute("read_file", path="missing.txt")
        with self.assertRaisesRegex(ToolError, "Failed"):
            self.manager.complete_task_step("first", [result["evidence_id"]], "done")
        (self.root / "missing.txt").write_text("found", encoding="utf-8")
        result = self.execute("read_file", path="missing.txt")
        self.manager.complete_task_step("first", [result["evidence_id"]], "read")
        with self.assertRaisesRegex(ToolError, "another step"):
            self.manager.complete_task_step("second", [result["evidence_id"]], "done")

    def test_full_file_check_rejects_truncated_read_until_all_chunks_exist(self):
        (self.root / "long.txt").write_text("abcdef", encoding="utf-8")
        self.manager.plan_task([action("read", [tool("read_file", path="long.txt")])])
        first = self.execute("read_file", path="long.txt", length=2)
        with self.assertRaisesRegex(ToolError, "truncated"):
            self.manager.complete_task_step("read", [first["evidence_id"]], "partial")
        with self.assertRaisesRegex(ToolError, "offset 2"):
            self.execute("read_file", path="long.txt", offset=0, length=2)
        second = self.execute("read_file", path="long.txt", offset=2, length=4)
        self.manager.complete_task_step("read", [first["evidence_id"], second["evidence_id"]], "read all six characters")
        summaries = self.manager.task_summaries()["summaries"]
        self.assertEqual(summaries[0]["summary"], "read all six characters")

    def test_legacy_oversized_read_evidence_is_recovered_from_model_visible_offset(self):
        (self.root / "legacy.txt").write_text("x" * 15000, encoding="utf-8")
        self.config.read_chunk_chars = 40000
        self.manager.plan_task([action("read", [tool("read_file", path="legacy.txt")])])
        legacy = self.execute("read_file", path="legacy.txt")
        with self.assertRaisesRegex(ToolError, "truncated"):
            self.manager.complete_task_step("read", [legacy["evidence_id"]], "legacy partial transport")
        self.config.read_chunk_chars = 12000
        with self.assertRaisesRegex(ToolError, "offset 12000"):
            self.execute("read_file", path="legacy.txt", offset=0)
        tail = self.execute("read_file", path="legacy.txt", offset=12000)
        self.manager.complete_task_step("read", [legacy["evidence_id"], tail["evidence_id"]], "all visible")

    def test_command_nonzero_is_not_completion_evidence(self):
        self.registry.add("run_command", "test", lambda: {"exit_code": 9, "stdout": "", "timed_out": False})
        self.manager.plan_task([action("run", [tool("run_command")])])
        result = self.execute("run_command")
        self.assertTrue(result["ok"])
        with self.assertRaisesRegex(ToolError, "Failed"):
            self.manager.complete_task_step("run", [result["evidence_id"]], "success")

    def test_file_completion_checks_actual_artifact_and_final_rechecks_it(self):
        self.manager.plan_task([action("write", [tool("write_file", path="out.md"),
                                                 {"type": "file", "path": "out.md", "contains": ["## Result"]}])])
        bad = self.execute("write_file", path="out.md", content="no heading")
        with self.assertRaisesRegex(ToolError, "content is missing"):
            self.manager.complete_task_step("write", [bad["evidence_id"]], "done")
        good = self.execute("write_file", path="out.md", content="## Result\nVerified", overwrite_existing=True)
        self.manager.complete_task_step("write", [good["evidence_id"]], "saved")
        (self.root / "out.md").write_text("changed outside agent", encoding="utf-8")
        self.assertIn("changed", self.manager.finalize())
        self.assertNotEqual(self.manager.state["status"], "complete")

    def test_plan_cannot_be_replaced_or_closed_out_of_order(self):
        self.manager.plan_task([action("one", [{"type": "answer"}]), action("two", [{"type": "answer"}])])
        with self.assertRaises(ToolError):
            self.manager.plan_task([action("skip", [{"type": "answer"}])])
        with self.assertRaises(ToolError):
            self.manager.complete_task_step("two", [], "skip one")

    def test_foreach_requires_real_complete_discovery_and_handles_zero_items(self):
        self.manager.plan_task([{"id": "each", "title": "each", "kind": "foreach", "steps": [action("work", [{"type": "answer"}])]}])
        (self.root / "items.txt").write_text("a\nb\n", encoding="utf-8")
        small = self.execute("read_file", path="items.txt", length=1)
        with self.assertRaisesRegex(ToolError, "truncated"):
            self.manager.expand_task("each", small["evidence_id"], "content")
        with self.assertRaisesRegex(ToolError, "Unknown"):
            self.manager.expand_task("each", "E999999", "content")
        (self.root / "items.txt").write_text("", encoding="utf-8")
        empty = self.execute("read_file", path="items.txt")
        self.manager.expand_task("each", empty["evidence_id"], "content")
        self.assertIsNone(self.manager.finalize())

    def test_foreach_rejects_banner_lines_before_saving_children(self):
        self.manager.plan_task([{"id": "each", "title": "each", "kind": "foreach", "steps": [
            action("read", [tool("read_file", path="tools/{name}.json")])]}])
        (self.root / "tools").mkdir()
        (self.root / "tools/good.ada.json").write_text("{}", encoding="utf-8")
        (self.root / "items.txt").write_text("Changed files:\ngood.ada\nWritten to queue\n", encoding="utf-8")
        result = self.execute("read_file", path="items.txt")
        with self.assertRaisesRegex(ToolError, "human-readable command stdout"):
            self.manager.expand_task("each", result["evidence_id"], "content")
        self.assertFalse(self.manager.state["steps"][0]["expanded"])
        self.assertEqual(self.manager.state["steps"][0]["children"], [])

    def test_foreach_discovery_auto_completes_matching_selection_actions(self):
        self.manager.plan_task([{"id": "each", "title": "each", "kind": "foreach", "steps": [
            action("select", [tool("read_file", path="items.txt")]),
            action("work", [tool("read_file", path="{item}")])]}])
        (self.root / "items.txt").write_text("one.txt\ntwo.txt\n", encoding="utf-8")
        (self.root / "one.txt").write_text("one", encoding="utf-8")
        (self.root / "two.txt").write_text("two", encoding="utf-8")
        result = self.execute("read_file", path="items.txt")
        self.manager.expand_task("each", result["evidence_id"], "content")
        children = self.manager.state["steps"][0]["children"]
        self.assertEqual([children[0]["status"], children[2]["status"]], ["done", "done"])
        self.assertEqual(self.manager.current()["id"], "each.1.work")

    def test_resume_repairs_pending_selection_from_saved_expansion_evidence(self):
        self.manager.plan_task([{"id": "each", "title": "each", "kind": "foreach", "steps": [
            action("select", [tool("read_file", path="items.txt")]),
            action("work", [{"type": "answer"}])]}])
        (self.root / "items.txt").write_text("one\n", encoding="utf-8")
        result = self.execute("read_file", path="items.txt")
        self.manager.expand_task("each", result["evidence_id"], "content")
        child = self.manager.state["steps"][0]["children"][0]
        child.update(status="pending")
        child.pop("evidence", None)
        child.pop("summary", None)
        self.manager.block("old ordering failure")
        resumed = TaskManager(self.ws, self.directory, self.config.enabled_tools)
        resumed.resume(self.manager.state["id"])
        self.assertEqual(resumed.state["steps"][0]["children"][0]["status"], "done")
        self.assertEqual(resumed.current()["id"], "each.1.work")

    def test_plan_normalizes_edit_file_as_empty_old_text_creation(self):
        self.manager.plan_task([action("write", [tool("edit_file", path="output/{name}.md",
                                                        old_text="", new_text="<report>")])])
        self.assertEqual(self.manager.state["steps"][0]["checks"][0],
                         tool("write_file", path="output/{name}.md"))

    def test_workflow_coverage_is_inferred_from_exact_step_titles(self):
        requirements = [{"id": "workflow_1", "title": "Create Queue", "source": "PLAN.md", "scope": "once"},
                        {"id": "workflow_2", "title": "Read Change", "source": "PLAN.md", "scope": "repeat"}]
        manager = TaskManager(self.ws, self.directory, self.config.enabled_tools, requirements)
        manager.begin("follow workflow")
        manager.plan_task([
            action("queue", [{"type": "answer"}]) | {"title": "Create Queue"},
            {"id": "each", "title": "files", "kind": "foreach", "steps": [
                action("read", [{"type": "answer"}]) | {"title": "Read Change"}]},
        ])
        self.assertEqual(manager.state["steps"][0]["covers"], ["workflow_1"])
        self.assertEqual(manager.state["steps"][1]["template"][0]["covers"], ["workflow_2"])

    def test_resume_keeps_completed_steps_and_does_not_repeat_successful_mutation(self):
        self.manager.plan_task([action("write", [tool("write_file")]), action("finish", [{"type": "answer"}])])
        first = self.execute("write_file", path="out.txt", content="saved")
        task_id = self.manager.state["id"]
        resumed = TaskManager(self.ws, self.directory, self.config.enabled_tools)
        resumed.resume(task_id)
        event, cached = resumed.before_tool(Call("write_file", {"path": "out.txt", "content": "saved"}))
        self.assertIsNone(event)
        self.assertEqual(cached["evidence_id"], first["evidence_id"])
        self.assertTrue(cached["cached"])
        resumed.complete_task_step("write", [first["evidence_id"]], "saved")
        self.assertEqual(resumed.current()["id"], "finish")

    def test_history_lists_requests_and_accepts_number_selectors(self):
        first_id = self.manager.state["id"]
        self.manager.state["request"] = "first remembered request"
        self.manager._save()
        self.manager.begin("second remembered request")
        history = self.manager.history()
        self.assertEqual([entry["request"] for entry in history[:2]],
                         ["second remembered request", "first remembered request"])
        self.assertEqual(self.manager.historical_request("1"), "second remembered request")
        self.assertEqual(self.manager.historical_request("2"), "first remembered request")
        self.assertEqual(history[1]["task_id"], first_id)

    def test_completed_history_must_be_rerun_not_resumed(self):
        self.manager.plan_task([action("answer", [{"type": "answer"}])])
        self.manager.complete_task_step("answer", [], "done")
        self.assertIsNone(self.manager.finalize())
        with self.assertRaisesRegex(ToolError, "rerun"):
            self.manager.resume("1")

    def test_unknown_command_outcome_does_not_replay_on_resume(self):
        self.manager.plan_task([action("run", [tool("run_command")])])
        self.manager.before_tool(Call("run_command", {"command": "something"}))
        resumed = TaskManager(self.ws, self.directory, self.config.enabled_tools)
        with self.assertRaisesRegex(ToolError, "unknown outcome"):
            resumed.resume(self.manager.state["id"])

    def test_resume_exposes_saved_evidence_without_reexecution(self):
        self.manager.plan_task([action("read", [tool("read_file")])])
        (self.root / "input.txt").write_text("original content", encoding="utf-8")
        result = self.execute("read_file", path="input.txt")
        resumed = TaskManager(self.ws, self.directory, self.config.enabled_tools)
        resumed.resume(self.manager.state["id"])
        self.assertEqual(resumed.status()["recent_evidence"][0]["id"], result["evidence_id"])
        saved = resumed.task_evidence(result["evidence_id"])
        self.assertEqual(saved["result"]["result"]["content"], "original content")

    def test_resume_recovers_missing_write_artifact_from_evidence(self):
        self.manager.plan_task([action("write", [tool("write_file")]),
                                action("finish", [{"type": "answer"}])])
        result = self.execute("write_file", path="output/report.md", content="durable report")
        self.manager.complete_task_step("write", [result["evidence_id"]], "saved")
        (self.root / "output/report.md").unlink()
        (self.root / "output").rmdir()
        resumed = TaskManager(self.ws, self.directory, self.config.enabled_tools)
        resumed.resume(self.manager.state["id"])
        self.assertEqual((self.root / "output/report.md").read_text(), "durable report")
        self.assertEqual(resumed.status()["recovered_artifacts"],
                         [str((self.root / "output/report.md").resolve())])

    def test_changed_file_cannot_return_cached_write_success(self):
        self.manager.plan_task([action("write", [tool("write_file")])])
        self.execute("write_file", path="out.txt", content="saved")
        (self.root / "out.txt").write_text("external change", encoding="utf-8")
        with self.assertRaisesRegex(ToolError, "changed externally"):
            self.execute("write_file", path="out.txt", content="saved")

    def test_generated_file_drops_speculative_plan_contains(self):
        self.manager.plan_task([action("write", [
            tool("write_file", path="odd.py", content="print('hello')"),
            {"type": "file", "path": "odd.py", "contains": ["def"]}])])
        checks = self.manager.current()["checks"]
        file_check = next(item for item in checks if item["type"] == "file")
        self.assertNotIn("contains", file_check)

    def test_later_read_does_not_hide_corrupted_output(self):
        self.manager.plan_task([action("write", [{"type": "file", "path": "out.txt"}]),
                                action("read", [tool("read_file")])])
        saved = self.execute("write_file", path="out.txt", content="valid")
        self.manager.complete_task_step("write", [saved["evidence_id"]], "saved")
        (self.root / "out.txt").write_text("corrupted", encoding="utf-8")
        read = self.execute("read_file", path="out.txt")
        self.manager.complete_task_step("read", [read["evidence_id"]], "read")
        self.assertIn("changed", self.manager.finalize())

    def test_generic_once_repeat_then_once_with_real_files_and_premature_answer(self):
        (self.root / "items.txt").write_text("one.c\ntwo.py\n", encoding="utf-8")
        (self.root / "one.c").write_text("int n = 1;", encoding="utf-8")
        (self.root / "two.py").write_text("n = 2", encoding="utf-8")
        steps = [action("setup", [tool("write_file", path="setup.txt")]),
                 action("discover", [tool("read_file", path="items.txt")]),
                 {"id": "each", "title": "process each", "kind": "foreach", "steps": [
                     action("read", [tool("read_file", path="{item}")]),
                     action("save", [tool("write_file", path="output/{item}.md"),
                                     {"type": "file", "path": "output/{item}.md", "contains": ["## Result"]}])]},
                 action("finish", [tool("write_file", path="finished.txt")])]
        replies = [call("plan_task", steps=steps), call("write_file", path="setup.txt", content="once"),
                   call("complete_task_step", step_id="setup", evidence_ids=["E000001"], summary="setup once"),
                   call("read_file", path="items.txt"),
                   call("complete_task_step", step_id="discover", evidence_ids=["E000002"], summary="two items"),
                   call("tool.run_command", name="expand_task", arguments={
                       "step_id": "each", "evidence_id": "E000002", "field": "content", "format": "lines"})]
        number = 3
        for index, item in enumerate(["one.c", "two.py"], 1):
            replies += [call("read_file", path=item),
                        call("complete_task_step", step_id=f"each.{index}.read", evidence_ids=[f"E{number:06d}"], summary="read source"),
                        {"content": "## Result\nHere is the document; I am done."},
                        call("write_file", path=f"output/{item}.md", content="## Result\nDocumented " + item),
                        call("complete_task_step", step_id=f"each.{index}.save", evidence_ids=[f"E{number + 1:06d}"], summary="saved document")]
            number += 2
        replies += [call("write_file", path="finished.txt", content="all done"),
                    call("complete_task_step", step_id="finish", evidence_ids=[f"E{number:06d}"], summary="finalized once"),
                    {"content": "Completed both files and finalization."}]
        client = FakeClient(replies)
        agent = Agent(client, self.registry, self.config, "Test", Log(), emit=lambda _: None, tasks=self.manager)
        self.assertIn("Completed both", agent.run("setup once, discover, read and save each item, finalize once"))
        self.assertEqual(self.manager.state["status"], "complete")
        self.assertEqual((self.root / "finished.txt").read_text(), "all done")
        self.assertTrue((self.root / "output/one.c.md").is_file())
        self.assertTrue((self.root / "output/two.py.md").is_file())
        saved = json.loads((self.manager.folder / "plan.json").read_text())
        self.assertEqual(saved["steps"][2]["item_count"], 2)
        self.assertEqual(saved["next_event"], 8)

    def test_no_progress_stops_incomplete_instead_of_looping(self):
        config = Config(trace=False, max_stalled_steps=2)
        replies = [call("plan_task", steps=[action("answer", [{"type": "answer"}])])] + [call("task_status")] * 3
        agent = Agent(FakeClient(replies), self.registry, config, "Test", Log(), emit=lambda _: None, tasks=self.manager)
        with self.assertRaisesRegex(AgentError, "No plan progress"):
            agent.run("do work")
        self.assertEqual(self.manager.state["status"], "blocked")

    def test_context_checkpoint_preserves_current_step_and_saved_results(self):
        self.manager.plan_task([action("read", [tool("read_file")])])
        (self.root / "input.txt").write_text("verified input", encoding="utf-8")
        result = self.execute("read_file", path="input.txt")
        replies = [call("task_evidence", evidence_id=result["evidence_id"]),
                   call("complete_task_step", step_id="read", evidence_ids=[result["evidence_id"]],
                        summary="Input says verified input"), {"content": "verified input"}]
        emitted = []
        agent = Agent(FakeClient(replies), self.registry, Config(trace=False, max_context_chars=12000),
                      "Test", Log(), emit=emitted.append, tasks=self.manager)
        agent.history = [{"role": "user", "content": "old conversation " * 1000}]
        self.assertEqual(agent.run("Summarize input.txt", resume=True), "verified input")
        self.assertTrue(any("checkpoint" in line for line in emitted))
        self.assertEqual(self.manager.state["next_event"], 2)

    def test_plain_text_never_completes_an_unplanned_task(self):
        agent = Agent(FakeClient([{"content": "I did everything"}] * 3), self.registry,
                      Config(trace=False, max_repairs=1), "Test", Log(), emit=lambda _: None, tasks=self.manager)
        with self.assertRaisesRegex(AgentError, "retry limit"):
            agent.run("write a report")
        self.assertEqual(self.manager.state["status"], "blocked")

    def test_direct_mode_executes_without_model_generated_plan(self):
        registry = Registry()
        register_file_tools(registry, self.ws, self.config)
        manager = TaskManager(self.ws, self.directory, self.config.enabled_tools, direct_mode=True)
        manager.register(registry)
        client = FakeClient([
            call("write_file", path="odd.py", content="print('ok')"),
            call("read_file", path="odd.py"),
            {"content": "Created and verified odd.py."}])
        agent = Agent(client, registry, self.config, "Test", Log(), emit=lambda _: None, tasks=manager)
        self.assertEqual(agent.run("create odd.py"), "Created and verified odd.py.")
        self.assertEqual(manager.state["status"], "complete")
        self.assertEqual((self.root / "odd.py").read_text(), "print('ok')")
        names = {schema["function"]["name"] for schema in client.schemas[0]}
        self.assertNotIn("plan_task", names)
        self.assertNotIn("complete_task_step", names)

    def test_automatic_mode_plans_bulk_work_but_keeps_small_work_direct(self):
        bulk = TaskManager(self.ws, self.directory, self.config.enabled_tools, direct_mode=None)
        bulk.begin("Scan every file in this repository and write a comprehensive summary; don't miss any file")
        self.assertFalse(bulk.state["direct"])
        self.assertEqual(bulk.state["status"], "planning")
        small = TaskManager(self.ws, self.directory, self.config.enabled_tools, direct_mode=None)
        small.begin("Create odd.py and run it")
        self.assertTrue(small.state["direct"])
        self.assertEqual(small.state["status"], "active")

    def test_planning_rejects_broad_glob_when_request_names_exact_directory_levels(self):
        manager = TaskManager(self.ws, self.directory, self.config.enabled_tools, direct_mode=None)
        manager.begin("Scan the root directory every file and the src directory every file")
        with self.assertRaisesRegex(ToolError, "Do not use a broad"):
            manager.before_tool(Call("glob_files", {"pattern": "**/*"}))
        event, cached = manager.before_tool(Call("glob_files", {"pattern": "*", "directory": "."}))
        self.assertIsNone(cached)
        self.assertEqual(event, "E000001")

    def test_scoped_file_review_plan_is_host_built_with_every_discovered_file(self):
        (self.root / "src").mkdir()
        (self.root / "root.md").write_text("root", encoding="utf-8")
        (self.root / "src" / "a.adb").write_text("a", encoding="utf-8")
        (self.root / "src" / "b.ads").write_text("b", encoding="utf-8")
        registry = Registry()
        register_file_tools(registry, self.ws, self.config)
        manager = TaskManager(self.ws, self.directory, self.config.enabled_tools, direct_mode=None)
        manager.register(registry)
        manager.begin("Scan the root directory every file and src directory every file; write it in output.md; don't miss any")
        for name, arguments in (("glob_files", {"pattern": "*", "directory": "."}),
                                ("list_files", {"directory": "src"})):
            call = Call(name, arguments)
            event, _ = manager.before_tool(call)
            manager.after_tool(event, registry.execute(call))
        self.assertEqual(manager.state["status"], "active")
        groups = manager.state["steps"][:2]
        self.assertTrue(all(group["expanded"] for group in groups))
        items = [child["item"] for group in groups for child in group["children"]]
        self.assertIn("root.md", items)
        self.assertEqual([item for item in items if item.startswith("src/")], ["src/a.adb", "src/b.ads"])
        plan = (manager.folder / "plan.md").read_text(encoding="utf-8")
        self.assertIn("Read fully and summarize src/a.adb", plan)
        self.assertIn("Read fully and summarize src/b.ads", plan)

    def test_history_review_records_model_and_artifacts(self):
        registry = Registry()
        register_file_tools(registry, self.ws, self.config)
        manager = TaskManager(self.ws, self.directory, self.config.enabled_tools, direct_mode=True,
                              metadata={"model": "coder:7b"})
        manager.register(registry)
        manager.begin("create report")
        request = Call("write_file", {"path": "report.txt", "content": "done"})
        event, _ = manager.before_tool(request)
        manager.after_tool(event, registry.execute(request))
        self.assertIsNone(manager.finalize())
        entry = manager.history()[0]
        self.assertEqual(entry["model"], "coder:7b")
        self.assertEqual(entry["artifact_count"], 1)
        review = manager.review("latest")
        self.assertEqual(Path(review["files"][0]["path"]).name, "report.txt")
        self.assertEqual(manager.delete_history("latest"), entry["task_id"])
        self.assertNotIn(entry["task_id"], {item["task_id"] for item in manager.history()})

    def test_action_scoped_tools_exclude_unneeded_command_and_write_tools(self):
        self.manager.plan_task([action("read", [tool("read_file", path="input.txt")])])
        allowed = self.manager.allowed_tools()
        self.assertIn("read_file", allowed)
        self.assertNotIn("complete_task_step", allowed)
        self.assertNotIn("write_file", allowed)
        self.assertNotIn("run_command", allowed)

    def test_completed_plan_action_compacts_full_system_prompt(self):
        replies = [call("plan_task", steps=[action("answer", [{"type": "answer"}])]),
                   call("complete_task_step", step_id="answer", evidence_ids=[], summary="answered"),
                   {"content": "done"}]
        client = FakeClient(replies)
        agent = Agent(client, self.registry, self.config, "LONG SYSTEM " * 1000,
                      Log(), emit=lambda _: None, tasks=self.manager)
        self.assertEqual(agent.run("answer"), "done")
        self.assertGreater(len(client.requests[0][0]["content"]),
                           len(client.requests[1][0]["content"]) * 2)
        self.assertGreater(agent.token_usage()["checkpoints"], 0)
