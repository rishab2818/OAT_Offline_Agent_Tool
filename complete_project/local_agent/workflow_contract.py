"""Extract a small, deterministic coverage contract from selected Markdown plans."""
import re


HEADING = re.compile(r"^###\s+\d+[.)]?\s+(.+?)\s*$", re.M)
RETURN = re.compile(r"\breturn\s+to\s+step\s+(\d+)\b", re.I)


def workflow_contract(documents):
    """Return requirements the runtime plan must explicitly cover.

    Only an explicit ``## Workflow`` section is compiled. Other skill prose stays
    ordinary model guidance, so merely loading a document cannot invent work.
    """
    requirements = []
    for path, text in documents:
        # Continue to EOF: output-format examples inside workflows legitimately
        # contain level-two headings (for example ``## PDL``). Numbered level-
        # three headings are the unambiguous step boundary.
        match = re.search(r"^##\s+Workflow\s*$([\s\S]*)", text, re.I | re.M)
        if not match:
            continue
        section = match.group(1)
        headings = HEADING.findall(section)
        if not headings:
            continue
        loop = RETURN.search(section)
        repeat_from = int(loop.group(1)) if loop else None
        for index, title in enumerate(headings, 1):
            requirements.append({
                "id": f"workflow_{len(requirements) + 1}",
                "title": title.strip(),
                "source": str(path),
                "scope": "repeat" if repeat_from and index >= repeat_from else "once",
            })
    return requirements
