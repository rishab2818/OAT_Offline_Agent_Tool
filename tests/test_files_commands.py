import os
import shutil
import tempfile
import unittest
from pathlib import Path

from local_agent.command_tools import register_command_tools, shell_argv
from local_agent.config import Config
from local_agent.file_tools import register_file_tools
from local_agent.protocol import Call
from local_agent.registry import Registry
from local_agent.workspace import Workspace


class FileCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "workspace").mkdir()
        self.ws = Workspace(self.root / "workspace")
        self.config = Config()
        self.registry = Registry()
        register_file_tools(self.registry, self.ws, self.config)
        register_command_tools(self.registry, self.ws, self.config)

    def call(self, name, **kwargs):
        return self.registry.execute(Call(name, kwargs))

    def test_absolute_paths_outside_workspace_and_chunking(self):
        target = self.root / "notes.txt"
        self.assertTrue(self.call("write_file", path=str(target), content="alpha beta gamma")["ok"])
        found = self.call("find_files", directory=str(self.root), pattern="NOTES.TXT")["result"]
        self.assertEqual(found["paths"], [str(target)])
        read = self.call("read_file", path=str(target), length=5)["result"]
        self.assertEqual((read["content"], read["next_offset"]), ("alpha", 5))
        tail = self.call("read_file", path=str(target), offset=5)["result"]
        self.assertEqual(tail["content"], " beta gamma")
        self.assertFalse(tail["truncated"])
        too_large = self.call("read_file", path=str(target), length=self.config.read_chunk_chars + 1)
        self.assertFalse(too_large["ok"])

    def test_overwrite_is_explicit(self):
        self.call("write_file", path="note.md", content="original")
        self.assertFalse(self.call("write_file", path="note.md", content="new")["ok"])
        self.assertTrue(self.call("write_file", path="note.md", content="new", overwrite_existing=True)["ok"])

    def test_edit_rejects_ambiguous_match_and_preserves_crlf(self):
        target = self.ws.root / "sample.py"
        target.write_bytes(b"a = 1\r\na = 1\r\n")
        self.assertFalse(self.call("edit_file", path="sample.py", old_text="a = 1", new_text="a = 2")["ok"])
        self.assertEqual(target.read_bytes(), b"a = 1\r\na = 1\r\n")
        self.assertTrue(self.call("edit_file", path="sample.py", old_text="a = 1\n", new_text="a = 2\n", replace_all=True)["ok"])
        self.assertEqual(target.read_bytes(), b"a = 2\r\na = 2\r\n")

    def test_optional_workspace_restriction(self):
        self.config.file_access = "workspace"
        self.assertFalse(self.call("write_file", path=str(self.root / "outside.txt"), content="x")["ok"])
        self.assertFalse(self.call("write_file", path="../outside.txt", content="x")["ok"])
        self.assertTrue(self.call("write_file", path="inside.txt", content="x")["ok"])

    def test_glob_scans_workspace_with_hidden_files_and_pagination(self):
        (self.ws.root / "src" / "nested").mkdir(parents=True)
        (self.ws.root / "src" / "a.py").write_text("a", encoding="utf-8")
        (self.ws.root / "src" / "nested" / "b.py").write_text("b", encoding="utf-8")
        (self.ws.root / ".hidden.py").write_text("h", encoding="utf-8")
        (self.ws.root / "note.txt").write_text("n", encoding="utf-8")
        first = self.call("glob_files", pattern="**/*.py", limit=2)["result"]
        self.assertEqual(first["paths"], [".hidden.py", "src/a.py"])
        self.assertEqual(first["next_offset"], 2)
        self.assertEqual(first["total_matches"], 3)
        second = self.call("glob_files", pattern="**/*.py", offset=2, limit=2)["result"]
        self.assertEqual(second["paths"], ["src/nested/b.py"])
        self.assertIsNone(second["next_offset"])
        visible = self.call("glob_files", pattern="**/*", include_hidden=False)["result"]
        self.assertNotIn(".hidden.py", visible["paths"])
        root_only = self.call("glob_files", pattern="*")["result"]
        self.assertEqual(root_only["paths"], [".hidden.py", "note.txt"])
        src_only = self.call("glob_files", pattern="*", directory="src")["result"]
        self.assertEqual(src_only["paths"], ["src/a.py"])

    def test_glob_cannot_escape_workspace_or_follow_symlinks(self):
        outside = self.root / "outside.py"
        outside.write_text("secret", encoding="utf-8")
        for directory in ("..", str(self.root)):
            result = self.call("glob_files", pattern="**/*", directory=directory)
            self.assertFalse(result["ok"])
        self.assertFalse(self.call("glob_files", pattern="../*.py")["ok"])
        self.assertFalse(self.call("glob_files", pattern=str(outside))["ok"])
        link = self.ws.root / "outside-link.py"
        try:
            link.symlink_to(outside)
        except OSError:
            return
        result = self.call("glob_files", pattern="**/*.py")["result"]
        self.assertNotIn("outside-link.py", result["paths"])

    def test_glob_never_exposes_internal_directories(self):
        for name in (".git", ".local-agent"):
            (self.ws.root / name).mkdir()
            (self.ws.root / name / "secret.txt").write_text("x", encoding="utf-8")
        result = self.call("glob_files", pattern="**/*")["result"]
        self.assertEqual(result["paths"], [])

    def test_disabled_tools_are_not_callable(self):
        self.registry.select(["read_file", "list_files"])
        self.assertNotIn("run_command", self.registry.tools)
        self.assertNotIn("write_file", self.registry.tools)

    @unittest.skipUnless(shutil.which("powershell") or shutil.which("pwsh"), "PowerShell unavailable")
    def test_powershell_success_and_error_exit(self):
        result = self.call("run_command", command="Write-Output 'hello from powershell'", cwd=str(self.root))["result"]
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("hello from powershell", result["stdout"])
        error = self.call("run_command", command="[Console]::Error.WriteLine('failure'); exit 7")["result"]
        self.assertEqual(error["exit_code"], 7)
        self.assertIn("failure", error["stderr"])

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_cmd_output(self):
        result = self.call("run_command", command="echo cmd-ok", shell="cmd")["result"]
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("cmd-ok", result["stdout"])

    def test_bash_if_available(self):
        try:
            shell_argv("bash", "printf bash-ok")
        except ValueError:
            self.skipTest("Bash unavailable")
        result = self.call("run_command", command="printf bash-ok", shell="bash")["result"]
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"], "bash-ok")

    @unittest.skipUnless(shutil.which("powershell") or shutil.which("pwsh"), "PowerShell unavailable")
    def test_command_timeout(self):
        self.config.command_timeout_seconds = 1
        result = self.call("run_command", command="Start-Sleep -Seconds 30")["result"]
        self.assertTrue(result["timed_out"])
        self.assertNotEqual(result["exit_code"], 0)
