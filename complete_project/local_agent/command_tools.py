"""Local shell execution with output capture and time limits."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .registry import string
from .workspace import ToolError


def shell_argv(shell, command, bash_executable=None):
    if shell == "powershell":
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if not executable:
            raise ToolError("PowerShell is not installed or is not on PATH")
        prefix = "$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
        return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", prefix + command]
    if shell == "cmd":
        if os.name != "nt":
            raise ToolError("cmd is only supported on Windows")
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
    if shell == "bash":
        if bash_executable and not Path(bash_executable).is_file():
            raise ToolError(f"Configured Bash executable not found: {bash_executable}")
        candidates = [bash_executable] if bash_executable else []
        if os.name == "nt":
            candidates += [str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"),
                           str(Path(os.environ.get("LOCALAPPDATA", "C:/")) / "Programs/Git/bin/bash.exe")]
        candidates += [shutil.which("bash")]
        executable = next((c for c in candidates if c and Path(c).is_file()), None)
        if not executable:
            raise ToolError("Bash not found. Install Git Bash or set bash_executable in config.json")
        return [executable, "--noprofile", "--norc", "-c", command]
    raise ToolError("Unsupported shell")


def register_command_tools(registry, workspace, config):
    def run_command(command, shell="powershell", cwd="."):
        if not command.strip():
            raise ToolError("Command cannot be empty")
        directory = (workspace.root / Path(cwd).expanduser()).resolve()
        if not directory.is_dir():
            raise ToolError(f"Command working directory does not exist: {directory}")
        arguments = shell_argv(shell, command, config.bash_executable)
        timed_out = False
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            process = subprocess.Popen(arguments, cwd=directory, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, **kwargs)
            try:
                process.wait(timeout=config.command_timeout_seconds)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                timed_out = True
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   capture_output=True, check=False, timeout=15)
                else:
                    import signal
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=15)
                if isinstance(exc, KeyboardInterrupt):
                    raise
            def read_output(stream):
                stream.seek(0)
                data = stream.read(config.max_file_chars * 4 + 1)
                text = data.decode("utf-8", errors="replace")
                return text[:config.max_file_chars], len(text) > config.max_file_chars or len(data) > config.max_file_chars * 4
            out, out_cut = read_output(stdout)
            err, err_cut = read_output(stderr)
        return {"shell": shell, "cwd": str(directory), "exit_code": process.returncode,
                "stdout": out, "stderr": err, "timed_out": timed_out,
                "truncated": out_cut or err_cut}
    registry.add("run_command", "Execute a requested local command. Returns stdout, stderr and exit code. No interactive input.",
                 run_command, {"command": string("Command text in the selected shell"),
                               "shell": {"type": "string", "enum": ["powershell", "bash", "cmd"]},
                               "cwd": string("Working directory; relative to workspace or absolute")}, ["command"])
