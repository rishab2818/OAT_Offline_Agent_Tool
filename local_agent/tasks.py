"""Persistent runtime plans, evidence-backed completion, and repeated work.

The model proposes the plan; the host owns status, evidence IDs, and transitions.
No source language, queue filename, or output format is assumed here.
"""
import copy
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid
from pathlib import Path

from .registry import string
from .storage import private_path
from .task_schema import plan_steps_schema
from .workspace import ToolError, atomic_write


CONTROL_TOOLS = {"plan_task", "task_status", "task_evidence", "expand_task", "complete_task_step", "report_blocker"}
READ_TOOLS = {"read_file", "list_files", "find_files", "search_text"}


def file_digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(262144), b""):
            value.update(chunk)
    return value.hexdigest()


class TaskManager:
    def __init__(self, workspace, directory, enabled_tools, requirements=None, direct_mode=False, metadata=None):
        self.ws = workspace
        self.directory = private_path(directory, "tasks")
        self.directory.mkdir(exist_ok=True)
        self.enabled_tools = set(enabled_tools)
        self.requirements = list(requirements or [])
        self.direct_mode = bool(direct_mode)
        self.metadata = dict(metadata or {})
        self.state = None
        self.folder = None

    def begin(self, prompt):
        task_id = uuid.uuid4().hex
        self.folder = private_path(self.directory, task_id)
        self.folder.mkdir()
        (self.folder / "evidence").mkdir()
        direct = self.direct_mode
        steps = ([{"id": "execute_request", "title": "Execute requested task", "kind": "action",
                   "status": "pending", "checks": [], "evidence": [], "adaptive": True}]
                 if direct else [])
        self.state = {"version": 1, "id": task_id, "workspace": str(self.ws.root),
                      "request": prompt, "status": "active" if direct else "planning", "steps": steps, "direct": direct,
                      "next_event": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                      "metadata": copy.deepcopy(self.metadata),
                      "revision": 0, "inflight": None, "reason": "",
                      "requirements": copy.deepcopy(self.requirements)}
        self._save()

    def history(self):
        """List saved tasks newest-first; numbering is accepted by resume/rerun."""
        entries = []
        paths = sorted(self.directory.glob("*/plan.json"),
                       key=lambda path: path.stat().st_mtime, reverse=True)
        for path in paths:
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                if state.get("workspace") != str(self.ws.root):
                    continue
                entries.append({"number": len(entries) + 1, "task_id": state.get("id", path.parent.name),
                                "status": state.get("status", "unknown"),
                                "request": state.get("request", ""),
                                "updated": path.stat().st_mtime,
                                "created_at": state.get("created_at"),
                                "model": state.get("metadata", {}).get("model"),
                                "artifact_count": len(self._artifacts_for(state, path.parent)["files"]),
                                "remaining_count": sum(s.get("status") != "done"
                                                       for s in self._walk_state(state.get("steps", [])))})
            except (OSError, ValueError, TypeError):
                continue
        return entries

    @staticmethod
    def _walk_state(steps):
        for step in steps:
            yield step
            yield from TaskManager._walk_state(step.get("children", []))

    def _resolve_history_selector(self, selector):
        selector = str(selector or "latest").strip().lower()
        entries = self.history()
        if not entries:
            raise ToolError("No saved tasks in this workspace")
        if selector == "latest":
            return entries[0]["task_id"]
        if selector.isdigit():
            number = int(selector)
            if number < 1 or number > len(entries):
                raise ToolError(f"History number must be between 1 and {len(entries)}")
            return entries[number - 1]["task_id"]
        if not re.fullmatch(r"[a-f0-9]{32}", selector):
            raise ToolError("Use a history number, 'latest', or a 32-character task ID")
        return selector

    def historical_request(self, selector="latest"):
        task_id = self._resolve_history_selector(selector)
        path = private_path(self.directory / task_id, "plan.json")
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("workspace") != str(self.ws.root):
            raise ToolError("Saved task belongs to another workspace")
        return state["request"]

    def resume(self, task_id="latest"):
        task_id = self._resolve_history_selector(task_id)
        self.folder = private_path(self.directory, task_id)
        state_path = private_path(self.folder, "plan.json")
        self.state = json.loads(state_path.read_text(encoding="utf-8"))
        self.requirements = self.state.get("requirements", [])
        if self.state["workspace"] != str(self.ws.root):
            raise ToolError("Saved task belongs to another workspace")
        recovered = self._recover_missing_written_files()
        if recovered:
            self.state["recovered_artifacts"] = recovered
        if self.state["status"] == "complete":
            raise ToolError("That task is already complete; use /rerun to run its request again")
        if self.state["inflight"]:
            event = self._event(self.state["inflight"])
            if event["tool"] not in READ_TOOLS:
                raise ToolError("Interrupted action has an unknown outcome. Inspect " +
                                str(self.folder / "evidence" / (event["id"] + ".json")) +
                                ". Start a new task with the observed state; it will not be replayed automatically.")
            self.state["inflight"] = None
        if self.state["status"] != "complete":
            self.state["status"] = "active" if self.state["steps"] else "planning"
            self.state["reason"] = ""
        if self._reconcile_expansion_evidence():
            self._advance()
        else:
            self._save()
        return self.state["request"]

    def _recover_missing_written_files(self):
        """Recover missing task-created files from durable write evidence.

        Never overwrite an existing file: external modifications remain visible
        and are handled by digest verification. This is only a missing-artifact
        recovery path for write_file calls whose exact content was persisted.
        """
        if not self.state:
            return []
        latest = {}
        for number in range(1, self.state.get("next_event", 1)):
            try:
                event = self._event(f"E{number:06d}")
            except ToolError:
                continue
            if self._successful(event) and event.get("tool") in {"write_file", "edit_file"} and event.get("file"):
                latest[event["file"]["path"]] = event
        recovered = []
        for path_text, event in latest.items():
            path = Path(path_text)
            if path.exists() or event["tool"] != "write_file":
                continue
            content = event.get("arguments", {}).get("content")
            if not isinstance(content, str):
                continue
            atomic_write(path, content)
            if file_digest(path) != event["file"]["sha256"]:
                raise ToolError("Recovered artifact digest mismatch: " + str(path))
            recovered.append(str(path))
        return recovered

    def _save(self):
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write(private_path(self.folder, "plan.json"), json.dumps(self.state, indent=2, ensure_ascii=False) + "\n")
        rows = [f"# Task {self.state['id']}", "", self.state["request"], "", f"Status: {self.state['status']}", ""]
        def render(steps, indent=""):
            for step in steps:
                mark = "x" if step["status"] == "done" else " "
                rows.append(f"{indent}- [{mark}] {step['id']}: {step['title']}")
                if step.get("summary"):
                    rows.append(f"{indent}  Result: {step['summary']}")
                if step.get("evidence"):
                    rows.append(f"{indent}  Evidence: {', '.join(step['evidence'])}")
                render(step.get("children", []), indent + "  ")
        render(self.state["steps"])
        if self.state["reason"]:
            rows += ["", "Incomplete: " + self.state["reason"]]
        atomic_write(private_path(self.folder, "plan.md"), "\n".join(rows) + "\n")

    def _artifacts_for(self, state, folder):
        files, commands = {}, []
        evidence_folder = folder / "evidence"
        for number in range(1, state.get("next_event", 1)):
            path = evidence_folder / f"E{number:06d}.json"
            try:
                event = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            result = event.get("result", {})
            if not result.get("ok"):
                continue
            tool = event.get("tool")
            if event.get("file"):
                file_path = event["file"]["path"]
                existing = files.get(file_path)
                operation = (existing.get("operation") if existing and tool == "read_file" else tool)
                files[file_path] = {"path": file_path, "operation": operation,
                                    "verified": tool == "read_file" or bool(existing and existing.get("verified")),
                                    "exists": Path(file_path).is_file(),
                                    "size_bytes": Path(file_path).stat().st_size if Path(file_path).is_file() else None,
                                    "sha256": event["file"].get("sha256"), "evidence_id": event.get("id")}
            if tool == "run_command":
                data = result.get("result", {})
                commands.append({"command": event.get("arguments", {}).get("command", ""),
                                 "cwd": event.get("arguments", {}).get("cwd"),
                                 "shell": event.get("arguments", {}).get("shell"),
                                 "exit_code": data.get("exit_code"), "timed_out": data.get("timed_out", False),
                                 "stdout": str(data.get("stdout", ""))[:1000],
                                 "stderr": str(data.get("stderr", ""))[:1000],
                                 "evidence_id": event.get("id")})
        return {"files": list(files.values()), "commands": commands}

    def review(self, selector=None):
        if selector is None and self.state:
            state, folder = self.state, self.folder
        else:
            task_id = self._resolve_history_selector(selector or "latest")
            folder = private_path(self.directory, task_id)
            state = json.loads(private_path(folder, "plan.json").read_text(encoding="utf-8"))
        artifacts = self._artifacts_for(state, folder)
        return {"task_id": state.get("id"), "status": state.get("status"),
                "request": state.get("request", ""), "model": state.get("metadata", {}).get("model"),
                "created_at": state.get("created_at"), "updated_at": state.get("updated_at"), **artifacts}

    def delete_history(self, selector):
        task_id = self._resolve_history_selector(selector)
        folder = private_path(self.directory, task_id)
        if self.folder and folder.resolve() == self.folder.resolve() and self.state and self.state.get("status") != "complete":
            raise ToolError("Cannot delete the active unfinished task")
        import shutil
        shutil.rmtree(folder)
        return task_id

    def _all(self, steps=None):
        for step in self.state["steps"] if steps is None else steps:
            yield step
            yield from self._all(step.get("children", []))

    def current(self):
        def first(steps):
            for step in steps:
                if step["status"] == "done":
                    continue
                if step["kind"] == "foreach" and step.get("expanded"):
                    return first(step["children"])
                return step
            return None
        return first(self.state["steps"])

    def status(self):
        if not self.state:
            return {"status": "not_started"}
        current = self.current()
        recent = [self._event(f"E{n:06d}") for n in
                  range(max(1, self.state["next_event"] - 10), self.state["next_event"])]
        return {"task_id": self.state["id"], "status": self.state["status"],
                "plan_file": str(self.folder / "plan.md"), "current": current,
                "plan": [{"id": s["id"], "title": s["title"], "kind": s["kind"],
                          "status": s["status"]} for s in self.state["steps"]],
                "recent_evidence": [{"id": e["id"], "step_id": e["step_id"],
                                     "tool": e["tool"], "successful": self._successful(e)} for e in recent],
                "completed": [s["id"] for s in self._all() if s["status"] == "done"][-10:],
                "completed_results": [{"id": s["id"], "summary": s.get("summary", ""),
                                       "evidence": s.get("evidence", [])}
                                      for s in self._all() if s["status"] == "done"][-10:],
                "remaining": [s["id"] for s in self._all() if s["status"] != "done"][:20],
                "remaining_count": sum(s["status"] != "done" for s in self._all()),
                "requirements": self.requirements,
                "recovered_artifacts": self.state.get("recovered_artifacts", []),
                "reason": self.state["reason"]}

    def task_evidence(self, evidence_id):
        """Retrieve host-recorded results after a restart, without executing again."""
        if not self.state:
            raise ToolError("No task loaded")
        return self._event(evidence_id)

    def _validate_steps(self, steps, depth=0):
        if depth > 1 or not isinstance(steps, list) or not 1 <= len(steps) <= 100:
            raise ToolError("Plan needs 1..100 steps; foreach groups contain action templates, not nested foreach groups")
        ids, clean = set(), []
        for raw in steps:
            if not isinstance(raw, dict) or set(raw) - {"id", "title", "kind", "checks", "steps", "covers"}:
                raise ToolError("Step fields: id, title, kind, covers, checks (action) or steps (foreach)")
            key, title, kind = raw.get("id"), raw.get("title"), raw.get("kind", "action")
            if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key) or key in ids:
                raise ToolError("Step IDs must be unique lowercase identifiers")
            if not isinstance(title, str) or not title.strip() or kind not in {"action", "foreach"}:
                raise ToolError("Each step needs a title and kind action or foreach")
            ids.add(key)
            step = {"id": key, "title": title, "kind": kind, "status": "pending"}
            covers = raw.get("covers", [])
            if not isinstance(covers, list) or not all(isinstance(v, str) for v in covers):
                raise ToolError("covers must be a list of workflow requirement IDs")
            # Small local models often reproduce the workflow titles correctly
            # but omit the bookkeeping-only covers field. Exact title matching is
            # deterministic and cannot claim coverage for differently named work.
            inferred = [r["id"] for r in self.requirements
                        if r["title"].strip().casefold() == title.strip().casefold()]
            covers = list(dict.fromkeys([*covers, *inferred]))
            if covers:
                step["covers"] = list(covers)
            if kind == "foreach":
                if "checks" in raw:
                    raise ToolError("Put checks on the steps inside a foreach group")
                step.update(template=self._validate_steps(raw.get("steps"), depth + 1), expanded=False, children=[])
            else:
                if "steps" in raw:
                    raise ToolError("An action cannot contain child steps")
                checks = raw.get("checks")
                if not isinstance(checks, list) or not checks:
                    raise ToolError("Every action needs at least one completion check")
                normalized_checks = []
                proposed_write_content = {}
                for supplied in checks:
                    if not isinstance(supplied, dict):
                        raise ToolError("Checks must be objects")
                    check = copy.deepcopy(supplied)
                    kind_check = check.get("type")
                    allowed = {"tool": {"type", "name", "arguments"},
                               "file": {"type", "path", "contains", "empty"}, "answer": {"type"}}
                    if kind_check not in allowed or set(check) - allowed[kind_check]:
                        raise ToolError("Check types: tool(name, arguments), file(path, contains, empty), answer")
                    if kind_check == "tool" and (check.get("name") not in self.enabled_tools
                                                 or not isinstance(check.get("arguments", {}), dict)):
                        raise ToolError("Tool check must name an enabled tool with object arguments")
                    if kind_check == "tool" and check.get("name") == "write_file":
                        # Content belongs to the later execution call, not to the
                        # immutable plan constraint. Keep only stable arguments.
                        arguments = check.setdefault("arguments", {})
                        if isinstance(arguments.get("path"), str) and isinstance(arguments.get("content"), str):
                            proposed_write_content[arguments["path"]] = arguments["content"]
                        arguments.pop("content", None)
                    if kind_check == "tool" and check.get("name") == "edit_file":
                        arguments = check.get("arguments", {})
                        if arguments.get("old_text") == "":
                            # edit_file itself rejects empty old_text. Models use
                            # this pattern to mean "create this document later";
                            # normalize that intent to the executable tool.
                            check["name"] = "write_file"
                            check["arguments"] = ({"path": arguments["path"]}
                                                  if isinstance(arguments.get("path"), str) else {})
                        replacement = arguments.get("new_text")
                        if check.get("name") == "edit_file" and isinstance(replacement, str) and re.search(r"<[^>]+>", replacement):
                            check["arguments"].pop("new_text", None)
                    if kind_check == "file":
                        if not isinstance(check.get("path"), str) or not check["path"]:
                            raise ToolError("File check needs a path")
                        if not isinstance(check.get("contains", []), list) or not all(isinstance(v, str) for v in check.get("contains", [])):
                            raise ToolError("File contains must be a list of strings")
                        if "empty" in check and type(check["empty"]) is not bool:
                            raise ToolError("File empty must be boolean")
                    normalized_checks.append(check)
                # Drop only assertions contradicted by the model's own proposed
                # write. This catches invented requirements such as "def" for a
                # valid top-level script, while retaining deliberate output-format
                # checks and all checks whose content is not yet known.
                for item in normalized_checks:
                    proposed = proposed_write_content.get(item.get("path"))
                    if item.get("type") == "file" and proposed is not None and "contains" in item:
                        item["contains"] = [value for value in item["contains"] if value in proposed]
                        if not item["contains"]:
                            item.pop("contains")
                step["checks"] = normalized_checks
            clean.append(step)
        return clean

    def plan_task(self, steps):
        self._active()
        if self.state["steps"]:
            raise ToolError("A plan already exists. It cannot be replaced to skip unfinished work.")
        proposed = self._validate_steps(steps)
        if self.requirements:
            known = {r["id"] for r in self.requirements}
            covered, repeat_covered = set(), set()
            def collect(items, inside_foreach=False):
                for item in items:
                    here = inside_foreach or item["kind"] == "foreach"
                    for requirement in item.get("covers", []):
                        if requirement not in known:
                            raise ToolError("Unknown workflow requirement ID: " + requirement)
                        covered.add(requirement)
                        if here:
                            repeat_covered.add(requirement)
                    collect(item.get("template", []), here)
            collect(proposed)
            missing = known - covered
            missing_repeat = {r["id"] for r in self.requirements if r["scope"] == "repeat"} - repeat_covered
            if missing or missing_repeat:
                details = []
                if missing:
                    details.append("not covered: " + ", ".join(sorted(missing)))
                if missing_repeat:
                    details.append("must be covered inside foreach: " + ", ".join(sorted(missing_repeat)))
                raise ToolError("Plan does not cover the complete selected workflow (" + "; ".join(details) + ")")
        self.state["steps"] = proposed
        self.state["status"] = "active"
        self.state["revision"] += 1
        self._save()
        return self.status()

    def _event(self, event_id):
        if not isinstance(event_id, str) or not re.fullmatch(r"E\d{6}", event_id):
            raise ToolError("Use an evidence_id returned by a real tool call")
        path = private_path(self.folder / "evidence", event_id + ".json")
        if not path.is_file():
            raise ToolError("Unknown evidence ID: " + event_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def _successful(self, event):
        result = event.get("result", {})
        if not result.get("ok"):
            return False
        data = result.get("result", {})
        return not (event["tool"] == "run_command" and
                    (data.get("exit_code") != 0 or data.get("timed_out")))

    def expand_task(self, step_id, evidence_id, field, format="lines"):
        self._active()
        step = self.current()
        if not step or step["id"] != step_id or step["kind"] != "foreach" or step["expanded"]:
            raise ToolError("Only the current unexpanded foreach step can be expanded")
        event = self._event(evidence_id)
        if not self._successful(event):
            raise ToolError("Cannot expand from a failed tool result")
        data = event["result"]["result"]
        if isinstance(data, dict) and data.get("truncated"):
            raise ToolError("Item discovery was truncated; obtain a complete result first")
        value = data
        for part in field.split(".") if field else []:
            if not isinstance(value, dict) or part not in value:
                raise ToolError("Source field does not exist in tool result: " + field)
            value = value[part]
        if format == "lines":
            if not isinstance(value, str):
                raise ToolError("lines requires text")
            items = [line.strip() for line in value.splitlines() if line.strip()]
        elif format == "json":
            items = json.loads(value) if isinstance(value, str) else value
        elif format == "paths":
            if not isinstance(value, list):
                raise ToolError("paths requires a list from find_files or list_files")
            items = [item.get("path") if isinstance(item, dict) else item for item in value]
        else:
            raise ToolError("format must be lines, json or paths")
        if not isinstance(items, list) or not all(isinstance(item, str) and item for item in items):
            raise ToolError("Discovery must yield a list of nonempty strings")
        if len(items) > 1000 or len(set(items)) != len(items):
            raise ToolError("Discovery list must be unique and contain at most 1000 items")
        def instantiate(value, item, index):
            if isinstance(value, str):
                return value.replace("{item}", item).replace("{name}", Path(item).name).replace("{index}", str(index))
            if isinstance(value, list):
                return [instantiate(v, item, index) for v in value]
            if isinstance(value, dict):
                return {k: instantiate(v, item, index) for k, v in value.items()}
            return value
        children = []
        for index, item in enumerate(items, 1):
            for template in step["template"]:
                child = instantiate(template, item, index)
                child["id"] = f"{step_id}.{index}.{template['id']}"
                child["item"] = item
                children.append(child)
        # Reject noisy discovery sources before persisting hundreds of invalid
        # children. Inputs which a repeated action promises to read must exist
        # now; generated outputs are not read_file inputs at this stage.
        missing = []
        planned_writes = {check.get("arguments", {}).get("path")
                          for child in children for check in child.get("checks", [])
                          if check.get("type") == "tool" and check.get("name") == "write_file"}
        for child_index, child in enumerate(children):
            template = step["template"][child_index % len(step["template"])]
            for check_index, check in enumerate(child.get("checks", [])):
                if check.get("type") != "tool" or check.get("name") != "read_file":
                    continue
                path = check.get("arguments", {}).get("path")
                original_path = template.get("checks", [])[check_index].get("arguments", {}).get("path")
                item_dependent = isinstance(original_path, str) and any(
                    marker in original_path for marker in ("{item}", "{name}", "{index}"))
                if isinstance(path, str) and item_dependent and path not in planned_writes:
                    target = (self.ws.root / path).resolve()
                    if not target.is_file():
                        missing.append({"item": child.get("item"), "path": path})
        if missing:
            sample = ", ".join(f"{entry['item']!r} -> {entry['path']!r}" for entry in missing[:3])
            raise ToolError("Foreach discovery produced items whose planned read inputs do not exist: " + sample +
                            ". Expand from a clean machine-readable result (for example the queue file content), "
                            "not human-readable command stdout.")
        step.update(expanded=True, children=children, evidence=[evidence_id], item_count=len(items))
        self._reconcile_expansion_evidence()
        self._advance()
        return self.status()

    def _reconcile_expansion_evidence(self):
        """Close child actions already proven by the foreach discovery read.

        A common plan shape repeats a "Select File" action whose only check is
        the same queue read used to expand the foreach. Requiring the model to
        replay and close that read for every item creates an artificial ordering
        trap. The parent evidence already proves each selected item.
        """
        if not self.state:
            return False
        changed = False
        for group in self._all():
            if group.get("kind") != "foreach" or not group.get("expanded") or not group.get("evidence"):
                continue
            events = [self._event(key) for key in group["evidence"]]
            for child in group.get("children", []):
                if child.get("status") == "done" or child.get("kind") != "action":
                    continue
                checks = child.get("checks", [])
                if not checks or any(check.get("type") != "tool" for check in checks):
                    continue
                matching = [event for event in events if self._successful(event) and
                            all(event["tool"] == check["name"] and
                                self._matches(event["arguments"], check.get("arguments", {}))
                                for check in checks)]
                if matching:
                    child.update(status="done", evidence=[matching[0]["id"]],
                                 summary=f"Selected discovered item {child.get('item', '')}".strip())
                    changed = True
        return changed

    def before_tool(self, call):
        if self.state["status"] in {"blocked", "complete"}:
            raise ToolError("Task is " + self.state["status"])
        if not self.state["steps"] and call.name not in READ_TOOLS:
            raise ToolError("Create the runtime plan with plan_task before changing files or executing commands")
        if self.state["steps"] and self.current() is None:
            raise ToolError("All plan steps are done; return the final answer")
        step = self.current()
        step_id = step["id"] if step else "discovery"
        if call.name in {"write_file", "edit_file"} and step and not step.get("adaptive"):
            allowed = False
            for check in step.get("checks", []):
                if check.get("type") == "tool" and check.get("name") == call.name:
                    allowed = self._matches(call.arguments, check.get("arguments", {}))
                elif check.get("type") == "file" and isinstance(call.arguments.get("path"), str):
                    allowed = ((self.ws.root / call.arguments["path"]).resolve() ==
                               (self.ws.root / check["path"]).resolve())
                if allowed:
                    break
            if not allowed:
                raise ToolError(f"{call.name} does not belong to current step {step_id}. "
                                "Complete the current step before modifying another artifact.")
        # A repeated identical successful action for the same step is returned
        # from evidence, not executed twice. Explicit repeated steps have new IDs.
        for number in (range(self.state["next_event"] - 1, 0, -1) if call.name not in READ_TOOLS else []):
            old = self._event(f"E{number:06d}")
            if old["step_id"] == step_id and old["tool"] == call.name and old["arguments"] == call.arguments and self._successful(old):
                if "file" in old:
                    path = Path(old["file"]["path"])
                    if not path.is_file() or file_digest(path) != old["file"]["sha256"]:
                        raise ToolError("Previously successful file action has changed externally. Inspect the file "
                                        "and explicitly repair it; cached success cannot be reused.")
                result = copy.deepcopy(old["result"])
                result.update(evidence_id=old["id"], cached=True)
                return None, result
        event_id = f"E{self.state['next_event']:06d}"
        self.state["next_event"] += 1
        event = {"id": event_id, "step_id": step_id, "tool": call.name,
                 "arguments": call.arguments, "status": "running"}
        atomic_write(private_path(self.folder / "evidence", event_id + ".json"), json.dumps(event, ensure_ascii=False))
        self.state["inflight"] = event_id
        self._save()
        return event_id, None

    def after_tool(self, event_id, result):
        event = self._event(event_id)
        event.update(status="finished", result=result)
        data = result.get("result", {})
        if self._successful(event) and event["tool"] in {"write_file", "edit_file", "read_file"}:
            path = data.get("path") if isinstance(data, dict) else None
            if path and Path(path).is_file():
                event["file"] = {"path": str(Path(path).resolve()), "sha256": file_digest(Path(path))}
        atomic_write(private_path(self.folder / "evidence", event_id + ".json"), json.dumps(event, ensure_ascii=False))
        self.state["inflight"] = None
        if self.state.get("direct") and self._successful(event):
            step = self.current()
            if step and step.get("adaptive"):
                step.setdefault("evidence", []).append(event_id)
                step["summary"] = f"Recorded successful {event['tool']} action"
                self.state["revision"] += 1
        self._save()
        return {**result, "evidence_id": event_id}

    def _matches(self, actual, expected):
        for key, value in expected.items():
            if key in {"path", "cwd", "directory"} and isinstance(value, str) and isinstance(actual.get(key), str):
                if (self.ws.root / actual[key]).resolve() != (self.ws.root / value).resolve():
                    return False
            elif actual.get(key) != value:
                return False
        return True

    def _check(self, step, events):
        for check in step["checks"]:
            if check["type"] == "tool":
                if not any(e["tool"] == check["name"] and self._matches(e["arguments"], check.get("arguments", {})) for e in events):
                    raise ToolError(f"No successful evidence for required tool: {check}")
            elif check["type"] == "file":
                path = (self.ws.root / check["path"]).resolve()
                # The check must be tied to an actual read/write of THIS file.
                linked = [e for e in events if e.get("file", {}).get("path") == str(path)]
                if not linked:
                    raise ToolError("File check needs read_file/write_file/edit_file evidence for " + str(path))
                if not path.is_file() or path.stat().st_size > self.ws.max_chars * 4:
                    raise ToolError("Required file is missing or exceeds verification limit: " + str(path))
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() not in {e["file"]["sha256"] for e in linked}:
                    raise ToolError("File changed since recorded tool evidence: " + str(path))
                content = data.decode("utf-8-sig")
                if check.get("empty") is True and content.strip():
                    raise ToolError("Required file must be empty: " + str(path))
                if check.get("empty") is not True and not content.strip():
                    raise ToolError("Required output file is empty: " + str(path))
                if any(text not in content for text in check.get("contains", [])):
                    raise ToolError("Required file content is missing: " + str(path))

    def complete_task_step(self, step_id, evidence_ids, summary):
        self._active()
        step = self.current()
        if not step or step["id"] != step_id or step["kind"] != "action":
            raise ToolError("Complete only the current action step")
        if not isinstance(summary, str) or not summary.strip():
            raise ToolError("A factual completion summary is required")
        if not isinstance(evidence_ids, list) or not all(isinstance(e, str) for e in evidence_ids):
            raise ToolError("evidence_ids must be a list of IDs")
        events = [self._event(key) for key in evidence_ids]
        if any(not self._successful(e) for e in events):
            raise ToolError("Failed/unfinished tool calls cannot prove completion")
        if any(e["step_id"] not in {step_id, "discovery"} for e in events):
            raise ToolError("Evidence belongs to another step")
        self._check(step, events)
        # Every actual file mutation is verified, even if the proposed plan
        # omitted an explicit file check. Store its expected state for finalize.
        for event in events:
            if event["tool"] in {"write_file", "edit_file"} and "file" in event:
                path = event["file"]["path"]
                if not any(c["type"] == "file" and str((self.ws.root / c["path"]).resolve()) == path
                           for c in step["checks"]):
                    step["checks"].append({"type": "file", "path": path,
                                           "empty": not Path(path).read_text(encoding="utf-8-sig").strip()})
        self._check(step, events)
        step.update(status="done", evidence=evidence_ids, summary=summary)
        self._advance()
        return self.status()

    def _active(self):
        if not self.state or self.state["status"] not in {"planning", "active"}:
            raise ToolError("Task is not active. Start or resume a task first.")

    def _advance(self):
        def close_groups(steps):
            for step in steps:
                if step["kind"] == "foreach" and step["expanded"]:
                    close_groups(step["children"])
                    if all(c["status"] == "done" for c in step["children"]):
                        step["status"] = "done"
        close_groups(self.state["steps"])
        self.state["revision"] += 1
        self._save()

    def finalize(self):
        if self.state.get("direct"):
            if self.state["status"] == "blocked":
                return "Task is blocked: " + self.state["reason"]
            # Recheck the latest recorded state of every file touched by the
            # task. Earlier versions are intentionally superseded by later
            # edits or reads of the same file.
            latest_files = {}
            step = self.state["steps"][0]
            for evidence_id in step.get("evidence", []):
                event = self._event(evidence_id)
                if event.get("file"):
                    latest_files[event["file"]["path"]] = event["file"]["sha256"]
            for path_text, digest in latest_files.items():
                path = Path(path_text)
                if not path.is_file() or file_digest(path) != digest:
                    return "A task file changed after its last recorded tool action: " + path_text
            step.update(status="done", summary=(step.get("summary") or "Completed request"))
            self.state["status"] = "complete"
            self.state["revision"] += 1
            self._save()
            return None
        if not self.state["steps"]:
            return "Create a plan_task covering the user's entire request first."
        if self.state["status"] == "blocked":
            return "Task is blocked: " + self.state["reason"]
        if self.current():
            return ("Task is unfinished. Plain text does not complete a step or write a file. "
                    "Use the tools, then complete_task_step with real evidence. Current: " + json.dumps(self.current()))
        # Recheck outputs at finalization, except files intentionally changed by
        # a later completed step (e.g. a shrinking queue).
        later_files = set()
        for step in reversed(list(self._all())):
            if step["kind"] != "action":
                continue
            events = [self._event(e) for e in step.get("evidence", [])]
            filtered = {**step, "checks": [c for c in step["checks"] if c["type"] != "file" or
                         str((self.ws.root / c["path"]).resolve()) not in later_files]}
            try:
                self._check(filtered, events)
            except ToolError as exc:
                return str(exc)
            later_files.update(str((self.ws.root / c["path"]).resolve())
                               for c in step["checks"] if c["type"] == "file")
        self.state["status"] = "complete"
        self._save()
        return None

    def block(self, reason):
        if self.state and self.state["status"] not in {"complete", "blocked"}:
            self.state.update(status="blocked", reason=reason)
            self._save()

    def report_blocker(self, reason):
        if not reason.strip():
            raise ToolError("Describe the actual blocker")
        self.block(reason)
        return self.status()

    def register(self, registry):
        contract = (" Required workflow coverage: " + json.dumps(self.requirements, ensure_ascii=False)
                    if self.requirements else "")
        registry.add("plan_task", "Save the full runtime plan. Each step has id, title, kind=action|foreach and optional covers. "
                     "Action checks: tool(name,arguments), file(path,contains,empty), or answer. "
                     "Foreach has steps templates using {item}/{name}/{index}; items come later from expand_task." + contract,
                     self.plan_task, {"steps": plan_steps_schema(self.enabled_tools)}, ["steps"])
        registry.add("task_status", "Read the saved plan, current step and completion status.", self.status)
        registry.add("task_evidence", "Retrieve a persisted real tool result by ID, including after resume. "
                     "Does not execute the tool again.", self.task_evidence,
                     {"evidence_id": string("Recorded evidence ID, e.g. E000001")}, ["evidence_id"])
        registry.add("expand_task", "Expand the current foreach using a real tool result; never supply invented items. "
                     "field is a dotted path inside result; format lines/json/paths.", self.expand_task,
                     {"step_id": string("Current foreach ID"), "evidence_id": string("Successful discovery tool evidence_id"),
                      "field": string("Result field, e.g. content, stdout, paths or items"),
                      "format": {"type": "string", "enum": ["lines", "json", "paths"]}},
                     ["step_id", "evidence_id", "field"])
        registry.add("complete_task_step", "Close ONLY the current action after its checks pass against recorded tool results.",
                     self.complete_task_step, {"step_id": string("Current action ID"),
                     "evidence_ids": {"type": "array"}, "summary": string("Factual result; for answer steps include the answer")},
                     ["step_id", "evidence_ids", "summary"])
        registry.add("report_blocker", "Record why work cannot safely continue. Leaves task incomplete, never marks it successful.",
                     self.report_blocker, {"reason": string("Concrete blocker or needed user input")}, ["reason"])
