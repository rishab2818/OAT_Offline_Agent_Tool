"""Workspace validation and atomic text-file writes."""
import os
import tempfile
from pathlib import Path


class ToolError(ValueError):
    pass


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".agent-tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Workspace:
    def __init__(self, root, max_chars=40000):
        self.root = Path(root).resolve()
        self.max_chars = max_chars
        if not self.root.is_dir():
            raise ToolError(f"Workspace does not exist: {self.root}")
