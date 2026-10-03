"""Route only instruction sections relevant to the current runtime action."""
import re
from pathlib import Path


def markdown_sections(text):
    matches = list(re.finditer(r"(?m)^(#{1,4})\s+(.+?)\s*$", text))
    if not matches:
        return [("", text.strip())] if text.strip() else []
    result = []
    prefix = text[:matches[0].start()].strip()
    if prefix:
        result.append(("", prefix))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result.append((match.group(2).strip(), text[match.start():end].strip()))
    return result


def _words(value):
    return {word for word in re.findall(r"[a-z0-9_]+", value.casefold()) if len(word) > 2}


class InstructionRouter:
    def __init__(self, documents=None, workspace=None):
        self.documents = [(Path(path), content) for path, content in (documents or [])]
        self.workspace = str(workspace or "")

    def for_action(self, current=None, direct=False):
        if direct:
            selected = [f"Instructions from {path.name}:\n{content.strip()}"
                        for path, content in self.documents if content.strip()]
            if self.workspace:
                selected.append("Workspace: " + self.workspace)
            return "\n\n".join(selected)
        if not self.documents:
            return f"Workspace: {self.workspace}" if self.workspace else ""
        title = (current or {}).get("title", "")
        title_words = _words(title)
        selected = []
        for path, content in self.documents:
            name = path.name.casefold()
            sections = markdown_sections(content)
            if "skill" in name and any(word in title.casefold() for word in
                                        ("analy", "write", "document", "generate", "review")):
                chosen = [body for _, body in sections]
            else:
                chosen = []
                for heading, body in sections:
                    heading_words = _words(heading)
                    if (heading.casefold() in {"rules", "scope", "constraints"}
                            or title_words & heading_words
                            or any(word in body.casefold() for word in title_words if len(word) > 4)):
                        chosen.append(body)
                if not chosen and len(content) <= 3000:
                    chosen = [content.strip()]
            if chosen:
                selected.append(f"Instructions from {path.name}:\n" + "\n\n".join(dict.fromkeys(chosen)))
        if self.workspace:
            selected.append("Workspace: " + self.workspace)
        return "\n\n".join(selected)
