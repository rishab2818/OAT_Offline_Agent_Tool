"""Explicit tool registration, argument validation and dispatch."""
import copy
from dataclasses import dataclass

from .workspace import ToolError


@dataclass
class Tool:
    name: str
    description: str
    properties: dict
    required: list[str]
    handler: object

    def schema(self):
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": copy.deepcopy(self.properties),
                           "required": list(self.required), "additionalProperties": False}}}


class Registry:
    def __init__(self):
        self.tools = {}

    def add(self, name, description, handler, properties=None, required=None):
        if name in self.tools:
            raise ValueError(f"Duplicate tool: {name}")
        self.tools[name] = Tool(name, description, properties or {}, required or [], handler)

    def schemas(self):
        return [tool.schema() for tool in self.tools.values()]

    def select(self, names):
        unknown = set(names) - set(self.tools)
        if unknown:
            raise ValueError(f"Unknown enabled tools: {sorted(unknown)}")
        self.tools = {name: self.tools[name] for name in names}

    def validate(self, call):
        if call.name not in self.tools:
            raise ToolError(f"Unknown tool {call.name!r}. Available: {', '.join(self.tools)}")
        tool = self.tools[call.name]
        unknown = set(call.arguments) - set(tool.properties)
        missing = set(tool.required) - set(call.arguments)
        if unknown or missing:
            raise ToolError(f"{call.name}: unknown arguments {sorted(unknown)}; missing {sorted(missing)}")
        types = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list}
        for key, value in call.arguments.items():
            schema = tool.properties[key]
            expected = types[schema["type"]]
            if type(value) is not expected:
                raise ToolError(f"{call.name}.{key} must be {schema['type']}")
            if "enum" in schema and value not in schema["enum"]:
                raise ToolError(f"{call.name}.{key} must be one of {schema['enum']}")

    def execute(self, call):
        self.validate(call)
        try:
            return {"ok": True, "result": self.tools[call.name].handler(**call.arguments)}
        except (ToolError, OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}


def string(description):
    return {"type": "string", "description": description}
