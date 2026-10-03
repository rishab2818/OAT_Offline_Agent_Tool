"""Model-visible plan schema; TaskManager remains the authoritative validator."""


def plan_steps_schema(enabled_tools, depth=0):
    check = {"anyOf": [
        {"type": "object", "properties": {
            "type": {"const": "tool"}, "name": {"type": "string", "enum": sorted(enabled_tools)},
            "arguments": {"type": "object", "description": "Only stable argument constraints, usually path. Do not put generated output content here."}},
         "required": ["type", "name"], "additionalProperties": False},
        {"type": "object", "properties": {
            "type": {"const": "file"}, "path": {"type": "string"},
            "contains": {"type": "array", "items": {"type": "string"}}, "empty": {"type": "boolean"}},
         "required": ["type", "path"], "additionalProperties": False},
        {"type": "object", "properties": {"type": {"const": "answer"}},
         "required": ["type"], "additionalProperties": False}]}
    common = {"id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,39}$"},
              "title": {"type": "string"},
              "covers": {"type": "array", "items": {"type": "string"},
                         "description": "Workflow requirement IDs covered by this step."}}
    variants = [{"type": "object", "properties": {
        **common, "kind": {"const": "action"},
        "checks": {"type": "array", "minItems": 1, "items": check}},
        "required": ["id", "title", "kind", "checks"], "additionalProperties": False}]
    # One foreach layer is sufficient for the supported runtime: a collection
    # expands into action templates. Recursive foreach groups multiplied this
    # schema several times and made local models produce truncated/malformed
    # tool arguments even for tiny user requests.
    if depth < 1:
        variants.append({"type": "object", "properties": {
            **common, "kind": {"const": "foreach"},
            "steps": plan_steps_schema(enabled_tools, depth + 1)},
            "required": ["id", "title", "kind", "steps"], "additionalProperties": False})
    return {"type": "array", "minItems": 1, "maxItems": 100, "items": {"anyOf": variants}}
