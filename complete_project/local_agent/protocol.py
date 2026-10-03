"""Normalize model tool calls without evaluating code or guessing arguments."""
import json
import re
from dataclasses import dataclass


class ProtocolError(ValueError):
    pass


@dataclass
class Call:
    name: str
    arguments: dict

    def native(self):
        return {"type": "function", "function": {
            "name": self.name, "arguments": self.arguments}}


FENCE = re.compile(r"^\s*(```|''')\s*(?:json)?\s*\n?([\s\S]*?)\s*\1\s*$", re.I)


def decode(value):
    """Unwrap fences and up to three JSON-string layers. Strict JSON throughout."""
    for _ in range(4):
        if not isinstance(value, str):
            return value
        value = value.strip()
        match = FENCE.fullmatch(value)
        if match:
            value = match.group(2)
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"Invalid JSON: {exc.msg} at character {exc.pos}") from exc
    if isinstance(value, str):
        raise ProtocolError("Too many nested JSON strings")
    return value


def normalize(value):
    value = decode(value)
    if isinstance(value, dict) and "tool_calls" in value:
        if set(value) != {"tool_calls"}:
            raise ProtocolError("Tool-call envelope must contain only tool_calls")
        value = decode(value["tool_calls"])
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not value or len(value) > 16:
        raise ProtocolError("Expected 1 to 16 tool calls")
    calls = []
    for item in value:
        if not isinstance(item, dict):
            raise ProtocolError("Each tool call must be an object")
        if "function" in item:
            if set(item) - {"function", "id", "type", "index"}:
                raise ProtocolError("Unexpected tool-call fields")
            fn = decode(item["function"])
        else:
            fn = item
        if not isinstance(fn, dict) or set(fn) - {"name", "arguments", "index"}:
            raise ProtocolError("Expected function name and arguments")
        name = fn.get("name")
        arguments = decode(fn.get("arguments", {}))
        # Some local chat templates encode a tool dispatch as this exact
        # wrapper. It is NOT a shell command: unwrap only the unambiguous
        # name/arguments pair and let the enabled-tool registry validate it.
        if name == "tool.run_command" and isinstance(arguments, dict) and set(arguments) == {"name", "arguments"}:
            name = arguments["name"]
            arguments = decode(arguments["arguments"])
        elif isinstance(name, str) and name.startswith(("tool.", "functions.")):
            name = name.split(".", 1)[1]
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ProtocolError("Invalid tool name")
        if not isinstance(arguments, dict):
            raise ProtocolError(f"{name}: arguments must be an object")
        calls.append(Call(name, arguments))
    return calls


def parse_message(message):
    """Native calls first. Text calls must occupy the ENTIRE message.

    Never scrape arbitrary JSON out of prose, examples, or thinking text.
    """
    if message.get("tool_calls"):
        return normalize(message["tool_calls"]), "native"
    content = message.get("content", "")
    if not isinstance(content, str):
        raise ProtocolError("Assistant content must be a string")
    stripped = content.strip()
    if not stripped:
        raise ProtocolError("Empty model reply; provide a tool call or final answer")
    candidate = stripped.startswith(("{", "[", "```", "'''", '"'))
    if candidate:
        try:
            value = decode(stripped)
        except ProtocolError:
            if re.search(r'"(?:tool_calls|function|arguments|name)"\s*:', stripped):
                raise
            return [], "text"
        def looks_like_call(item):
            return isinstance(item, dict) and ("tool_calls" in item or "function" in item
                                               or {"name", "arguments"} <= set(item))
        if looks_like_call(value) or (isinstance(value, list) and any(looks_like_call(item) for item in value)):
            return normalize(value), "text"
    # Recognize but NEVER execute a call embedded in prose; request a clean retry.
    if re.search(r'"(?:tool_calls|function)"\s*:', stripped):
        raise ProtocolError("Tool call embedded in prose; return only the JSON call")
    return [], "text"
