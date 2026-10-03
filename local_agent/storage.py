"""Local audit logs and an OS-released, per-workspace exclusive lock."""
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .workspace import ToolError


def private_directory(workspace):
    directory = workspace.root / ".local-agent"
    if directory.is_symlink() or directory.resolve() != directory:
        raise ToolError(".local-agent must be a real directory inside the workspace")
    directory.mkdir(exist_ok=True)
    return directory


def private_path(directory, name):
    target = directory / name
    if target.is_symlink() or target.resolve() != target:
        raise ToolError(f"Internal file must not be a symlink: {target}")
    return target


@contextmanager
def workspace_lock(directory):
    path = private_path(directory, "session.lock")
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ToolError("Another agent session is using this workspace") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class RunLog:
    def __init__(self, directory):
        name = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:8] + ".jsonl"
        self.path = private_path(directory, name)

    def write(self, event, **data):
        record = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **data}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
