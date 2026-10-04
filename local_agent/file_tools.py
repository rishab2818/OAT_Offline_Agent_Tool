"""Task-independent tools for plain text and source-code files."""
import fnmatch
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from .registry import string
from .workspace import ToolError, atomic_write


def register_file_tools(registry, workspace, config):
    excluded_directories = {".git", ".local-agent"}

    def resolve(path):
        if not path or "\x00" in path:
            raise ToolError("Path must be nonempty")
        if os.name == "nt":
            windows = PureWindowsPath(path)
            if windows.drive and not windows.root:
                raise ToolError("Use a full Windows path such as D:/folder/file.txt")
            if any(PureWindowsPath(p).is_reserved() or ":" in p or p.endswith((".", " "))
                   for p in windows.parts if p not in {windows.anchor, ".", ".."}):
                raise ToolError("Windows device names, alternate streams and ambiguous path names are unsupported")
        target = (workspace.root / Path(path).expanduser()).resolve()
        if config.file_access == "workspace" and not target.is_relative_to(workspace.root):
            raise ToolError("File access is restricted to the selected workspace")
        if any(p.casefold() in {".git", ".local-agent"} for p in target.parts):
            raise ToolError("Internal Git/agent files are not exposed by file tools")
        return target

    def read_file(path, offset=0, length=None):
        target = resolve(path)
        length = config.max_file_chars if length is None else length
        if offset < 0 or not 1 <= length <= config.max_file_chars:
            raise ToolError(f"offset must be >= 0; length must be 1..{config.max_file_chars}")
        with target.open(encoding="utf-8-sig") as stream:
            remaining = offset
            while remaining:
                skipped = stream.read(min(remaining, config.max_file_chars))
                if not skipped:
                    break
                remaining -= len(skipped)
            content = stream.read(length)
            more = bool(stream.read(1))
        return {"path": str(target), "content": content, "offset": offset,
                "next_offset": offset + len(content) if more else None, "truncated": more}

    def list_files(directory="."):
        base = resolve(directory)
        if not base.is_dir():
            raise ToolError("directory must be a directory")
        items = []
        for path in sorted(base.iterdir()):
            if path.name.casefold() in {".git", ".local-agent", "__pycache__", ".venv"}:
                continue
            items.append({"path": str(path), "type": "directory" if path.is_dir() else "file"})
            if len(items) >= 500:
                return {"items": items, "truncated": True}
        return {"items": items, "truncated": False}

    def find_files(directory, pattern, recursive=True):
        base = resolve(directory)
        if not base.is_dir():
            raise ToolError("directory must exist")
        matches, visited, errors = [], 0, []
        for root, dirs, files in os.walk(base, followlinks=False, onerror=lambda e: errors.append(str(e))):
            dirs[:] = [d for d in dirs if d.casefold() not in
                       {".git", ".local-agent", "__pycache__", ".venv", "node_modules"}
                       and not (Path(root) / d).is_symlink()]
            visited += 1
            for name in files:
                if fnmatch.fnmatchcase(name.casefold(), pattern.casefold()):
                    matches.append(str(Path(root) / name))
                    if len(matches) >= 200:
                        return {"paths": matches, "truncated": True, "errors": errors[:10]}
            if not recursive:
                break
            if visited >= 10000:
                return {"paths": matches, "truncated": True, "errors": errors[:10]}
        return {"paths": matches, "truncated": False, "errors": errors[:10]}

    def glob_files(pattern, directory=".", offset=0, limit=500, include_hidden=True):
        """Match all regular files below a workspace directory without escaping it."""
        if not pattern or "\x00" in pattern:
            raise ToolError("pattern must be nonempty")
        normalized = pattern.replace("\\", "/")
        pattern_path = PurePosixPath(normalized)
        windows_pattern = PureWindowsPath(pattern)
        if (pattern_path.is_absolute() or windows_pattern.is_absolute() or windows_pattern.drive
                or ".." in pattern_path.parts or ".." in windows_pattern.parts):
            raise ToolError("glob pattern must be relative and cannot contain '..'")
        raw_directory = Path(directory).expanduser()
        if raw_directory.is_absolute():
            raise ToolError("glob directory must be relative to the workspace")
        base = (workspace.root / raw_directory).resolve()
        if not base.is_relative_to(workspace.root):
            raise ToolError("glob cannot escape the workspace sandbox")
        if not base.is_dir():
            raise ToolError("glob directory must be an existing workspace directory")
        if offset < 0 or not 1 <= limit <= 1000:
            raise ToolError("offset must be >= 0; limit must be 1..1000")

        matches, errors, scanned = [], [], 0
        for root, dirs, files in os.walk(base, followlinks=False,
                                         onerror=lambda error: errors.append(str(error))):
            root_path = Path(root)
            dirs[:] = sorted(d for d in dirs
                              if d.casefold() not in excluded_directories
                              and not (root_path / d).is_symlink()
                              and (include_hidden or not d.startswith(".")))
            for name in sorted(files):
                path = root_path / name
                if path.is_symlink() or (not include_hidden and name.startswith(".")):
                    continue
                scanned += 1
                relative_base = path.relative_to(base).as_posix()
                candidate = PurePosixPath(relative_base)
                # PurePath treats **/ as requiring a directory on some Python
                # versions; the second match includes root-level files too.
                if "/" not in normalized:
                    matched = "/" not in relative_base and fnmatch.fnmatchcase(relative_base, normalized)
                else:
                    matched = candidate.match(normalized)
                if not matched and normalized.startswith("**/"):
                    matched = candidate.match(normalized[3:])
                if matched:
                    matches.append(path.relative_to(workspace.root).as_posix())
        matches.sort(key=str.casefold)
        page = matches[offset:offset + limit]
        next_offset = offset + len(page) if offset + len(page) < len(matches) else None
        return {"paths": page, "offset": offset, "next_offset": next_offset,
                "total_matches": len(matches), "scanned_files": scanned,
                "truncated": next_offset is not None, "errors": errors[:10]}

    def search_text(path, text):
        if not text:
            raise ToolError("Search text cannot be empty")
        target = resolve(path)
        matches, scanned = [], 0
        with target.open(encoding="utf-8-sig") as stream:
            for n, line in enumerate(stream, 1):
                scanned += len(line)
                if text.casefold() in line.casefold():
                    matches.append({"line": n, "text": line.rstrip()[:2000]})
                if len(matches) >= 100 or scanned > 10_000_000:
                    return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def write_file(path, content, overwrite_existing=False):
        target = resolve(path)
        if len(content) > workspace.max_chars:
            raise ToolError("Output exceeds size limit")
        if target.exists():
            if target.read_text(encoding="utf-8") == content:
                return {"path": str(target), "unchanged": True}
            if not overwrite_existing:
                raise ToolError("File exists. Set overwrite_existing=true only if the user requested replacement.")
        atomic_write(target, content)
        return {"path": str(target), "written": True}

    def edit_file(path, old_text, new_text, replace_all=False):
        target = resolve(path)
        if not old_text:
            raise ToolError("old_text cannot be empty; use write_file to create a file")
        if target.stat().st_size > workspace.max_chars * 4:
            raise ToolError("File exceeds edit size limit")
        with target.open(encoding="utf-8", newline="") as stream:
            original = stream.read()
        if len(original) > workspace.max_chars:
            raise ToolError("File exceeds edit size limit")
        # Reads use universal newlines, so accept the same text in CRLF files.
        newline = "\r\n" if "\r\n" in original else "\n"
        before = old_text.replace("\r\n", "\n").replace("\n", newline)
        after = new_text.replace("\r\n", "\n").replace("\n", newline)
        count = original.count(before)
        if not count or (count != 1 and not replace_all):
            raise ToolError(f"old_text matches {count} times; provide a unique exact match or set replace_all=true")
        updated = original.replace(before, after, -1 if replace_all else 1)
        if len(updated) > workspace.max_chars:
            raise ToolError("Edited file would exceed size limit")
        atomic_write(target, updated)
        return {"path": str(target), "replacements": count if replace_all else 1, "changed": updated != original}

    registry.add("list_files", "List one directory. Accepts absolute Windows paths or workspace-relative paths.", list_files,
                 {"directory": string("Directory path, default workspace")})
    registry.add("find_files", "Find files by filename or wildcard such as *.md; returns at most 200 paths.", find_files,
                 {"directory": string("Directory to search"), "pattern": string("Filename or wildcard"),
                  "recursive": {"type": "boolean"}}, ["directory", "pattern"])
    registry.add("glob_files", "Recursively glob every regular file inside the workspace sandbox. Supports **; use offset/next_offset for large result sets.", glob_files,
                 {"pattern": string("Workspace-relative glob such as **/*.py or src/**/test_*.py"),
                  "directory": string("Workspace-relative starting directory, default ."),
                  "offset": {"type": "integer"}, "limit": {"type": "integer"},
                  "include_hidden": {"type": "boolean"}}, ["pattern"])
    registry.add("read_file", "Read a UTF-8 file in character chunks. Follow next_offset when truncated.", read_file,
                 {"path": string("File path"), "offset": {"type": "integer"}, "length": {"type": "integer"}}, ["path"])
    registry.add("search_text", "Search literal text in one file, ignoring case.", search_text,
                 {"path": string("File path"), "text": string("Literal search text")}, ["path", "text"])
    registry.add("write_file", "Create a UTF-8 text file. Replace an existing file only when explicitly requested.", write_file,
                 {"path": string("Target path"), "content": string("Complete file content"),
                  "overwrite_existing": {"type": "boolean"}}, ["path", "content"])
    registry.add("edit_file", "Replace an exact text match in a UTF-8 file; reject ambiguous matches by default.", edit_file,
                 {"path": string("File path"), "old_text": string("Exact existing text"),
                  "new_text": string("Replacement text"), "replace_all": {"type": "boolean"}},
                 ["path", "old_text", "new_text"])
